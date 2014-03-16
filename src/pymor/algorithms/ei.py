# This file is part of the pyMOR project (http://www.pymor.org).
# Copyright Holders: Felix Albrecht, Rene Milk, Stephan Rave
# License: BSD 2-Clause License (http://opensource.org/licenses/BSD-2-Clause)

'''This module contains algorithms for the empirical interpolation of operators.

The main work for generating the necessary interpolation data is handled by
the :func:`ei_greedy` method. The objects returned by this method can be used
to instantiate an |EmpiricalInterpolatedOperator|.

:func:`ei_greedy` expects an iterable of operator evaluations which are to be
interpolated. These evaluation can be provided by an instance of
:class:`EvaluationProvider` which, given a discretization, names of |Operators|
and a set of parameters, provides evaluations of the |Operators| on the solution
snapshots for the given parameters. Caching of the evaluations is also
handled by :class:`EvaluationProvider`.

As a convenience, the :func:`interpolate_operators` method allows to perform
the empirical interpolation of the |Operators| of a given discretization with
a single function call.
'''

from __future__ import absolute_import, division, print_function

import numpy as np
from scipy.linalg import solve_triangular, cho_factor, cho_solve

from pymor.core import getLogger
from pymor.core.cache import CacheableInterface, cached
from pymor.la import VectorArrayInterface
from pymor.la.pod import pod
from pymor.operators.ei import EmpiricalInterpolatedOperator


def ei_greedy(evaluations, error_norm=None, target_error=None, max_interpolation_dofs=None,
              projection='orthogonal', product=None):
    '''Generate data for empirical operator interpolation by a greedy search (EI-Greedy algorithm).

    Given evaluations of |Operators|, this method generates a collateral_basis and
    interpolation DOFs for empirical operator interpolation. The returned objects
    can be used to instantiate an |EmpiricalInterpolatedOperator|.

    The interpolation data is generated by a greedy search algorithm, adding in each
    loop the worst approximated operator evaluation to the collateral basis.

    Parameters
    ----------
    evaluations
        An iterable of operator evaluations. Each element must be a |VectorArray|
        of the same type and dimension, but it can hold an arbitrary number of evaluations.
    error_norm
        Norm w.r.t. which to calculate the interpolation error. If `None`, the Euclidean norm
        is used.
    target_error
        Stop the greedy search if the largest approximation error is below this threshold.
    max_interpolation_dofs
        Stop the greedy search if the number of interpolation DOF (= dimension of the collateral
        basis) reaches this value.
    projection
        If `ei`, compute the approximation error by comparing the given evaluation by the
        evaluation of the interpolated operator. If `orthogonal`, compute the error by
        comparing with the orthogonal projection onto the span of the collateral basis.
    product
        If `projection == 'orthogonal'`, the product which is used to perform the projection.
        If `None`, the Euclidean product is used.

    Returns
    -------
    interpolation_dofs
        |NumPy array| of the DOFs at which the operators have to be evaluated.
    collateral_basis
        |VectorArray| containing the generated collateral basis.
    data
        Dict containing the following fields:

            :errors: sequence of maximum approximation errors during greedy search.
    '''

    assert projection in ('orthogonal', 'ei')
    assert isinstance(evaluations, VectorArrayInterface)\
        or all(isinstance(ev, VectorArrayInterface) for ev in evaluations)
    if isinstance(evaluations, VectorArrayInterface):
        evaluations = (evaluations,)

    logger = getLogger('pymor.algorithms.ei.ei_greedy')
    logger.info('Generating Interpolation Data ...')

    interpolation_dofs = np.zeros((0,), dtype=np.int32)
    interpolation_matrix = np.zeros((0, 0))
    collateral_basis = type(next(iter(evaluations))).empty(dim=next(iter(evaluations)).dim)
    max_errs = []
    triangularity_errs = []

    def interpolate(U, ind=None):
        coefficients = solve_triangular(interpolation_matrix, U.components(interpolation_dofs, ind=ind).T,
                                        lower=True, unit_diagonal=True).T
        # coefficients = np.linalg.solve(interpolation_matrix, U.components(interpolation_dofs, ind=ind).T).T
        return collateral_basis.lincomb(coefficients)

    # compute the maximum projection error and error vector for the current interpolation data
    def projection_error():
        max_err = -1.

        # precompute gramian_inverse if needed
        if projection == 'orthogonal' and len(interpolation_dofs) > 0:
            if product is None:
                gramian = collateral_basis.gramian()
            else:
                gramian = product.apply2(collateral_basis, collateral_basis, pairwise=False)
            gramian_cholesky = cho_factor(gramian, overwrite_a=True)

        for AU in evaluations:
            if len(interpolation_dofs) > 0:
                if projection == 'ei':
                    AU_interpolated = interpolate(AU)
                    ERR = AU - AU_interpolated
                else:
                    if product is None:
                        coefficients = cho_solve(gramian_cholesky,
                                                 collateral_basis.dot(AU, pairwise=False)).T
                    else:
                        coefficients = cho_solve(gramian_cholesky,
                                                 product.apply2(collateral_basis, AU, pairwise=False)).T
                    AU_projected = collateral_basis.lincomb(coefficients)
                    ERR = AU - AU_projected
            else:
                ERR = AU
            errs = ERR.l2_norm() if error_norm is None else error_norm(ERR)
            local_max_err_ind = np.argmax(errs)
            local_max_err = errs[local_max_err_ind]
            if local_max_err > max_err:
                max_err = local_max_err
                if len(interpolation_dofs) == 0 or projection == 'ei':
                    new_vec = ERR.copy(ind=local_max_err_ind)
                else:
                    new_vec = AU.copy(ind=local_max_err_ind)
                    new_vec -= interpolate(AU, ind=local_max_err_ind)

        return max_err, new_vec

    # main loop
    while True:
        max_err, new_vec = projection_error()

        logger.info('Maximum interpolation error with {} interpolation DOFs: {}'.format(len(interpolation_dofs),
                                                                                        max_err))
        if target_error is not None and max_err <= target_error:
            logger.info('Target error reached! Stopping extension loop.')
            break

        # compute new interpolation dof and collateral basis vector
        new_dof = new_vec.amax()[0][0]
        if new_dof in interpolation_dofs:
            logger.info('DOF {} selected twice for interplation! Stopping extension loop.'.format(new_dof))
            break
        new_vec *= 1 / new_vec.components([new_dof])[0, 0]
        interpolation_dofs = np.hstack((interpolation_dofs, new_dof))
        collateral_basis.append(new_vec, remove_from_other=True)
        interpolation_matrix = collateral_basis.components(interpolation_dofs).T
        max_errs.append(max_err)

        triangularity_error = np.max(np.abs(interpolation_matrix - np.tril(interpolation_matrix)))
        triangularity_errs.append(triangularity_error)
        logger.info('Interpolation matrix is not lower triangular with maximum error of {}'
                    .format(triangularity_error))

        if len(interpolation_dofs) >= max_interpolation_dofs:
            logger.info('Maximum number of interpolation DOFs reached. Stopping extension loop.')
            max_err, _ = projection_error()
            logger.info('Final maximum interpolation error with {} interpolation DOFs: {}'.format(
                len(interpolation_dofs), max_err))
            break

        logger.info('')

    data = {'errors': max_errs, 'triangularity_errors': triangularity_errs}

    return interpolation_dofs, collateral_basis, data


def deim(evaluations, modes=None, error_norm=None, product=None):
    '''Generate data for empirical operator interpolation using DEIM algorithm.

    Given evaluations of |Operators|, this method generates a collateral_basis and
    interpolation DOFs for empirical operator interpolation. The returned objects
    can be used to instantiate an |EmpiricalInterpolatedOperator|.

    The collateral basis is determined by the first POD modes of the operator
    evaluations.

    Parameters
    ----------
    evaluations
        A |VectorArray| of operator evaluations.
    modes
        Dimension of the collateral basis i.e. number of POD modes of the operator evaluations.
    error_norm
        Norm w.r.t. which to calculate the interpolation error. If `None`, the Euclidean norm
        is used.
    product
        Product |Operator| used for POD.

    Returns
    -------
    interpolation_dofs
        |NumPy array| of the DOFs at which the operators have to be evaluated.
    collateral_basis
        |VectorArray| containing the generated collateral basis.
    data
        Dict containing the following fields:

            :errors: sequence of maximum approximation errors during greedy search.
    '''

    assert isinstance(evaluations, VectorArrayInterface)

    logger = getLogger('pymor.algorithms.ei.deim')
    logger.info('Generating Interpolation Data ...')

    collateral_basis = pod(evaluations, modes, product=product)

    interpolation_dofs = np.zeros((0,), dtype=np.int32)
    interpolation_matrix = np.zeros((0, 0))
    errs = []

    for i in xrange(len(collateral_basis)):

        if len(interpolation_dofs) > 0:
            coefficients = np.linalg.solve(interpolation_matrix,
                                           collateral_basis.components(interpolation_dofs, ind=i).T).T
            U_interpolated = collateral_basis.lincomb(coefficients, ind=range(len(interpolation_dofs)))
            ERR = collateral_basis.copy(ind=i)
            ERR -= U_interpolated
        else:
            ERR = collateral_basis.copy(ind=i)

        err = ERR.l2_norm() if error_norm is None else error_norm(ERR)

        logger.info('Interpolation error for basis vector {}: {}'.format(i, err))

        # compute new interpolation dof and collateral basis vector
        new_dof = ERR.amax()[0][0]

        if new_dof in interpolation_dofs:
            logger.info('DOF {} selected twice for interplation! Stopping extension loop.'.format(new_dof))
            break

        interpolation_dofs = np.hstack((interpolation_dofs, new_dof))
        interpolation_matrix = collateral_basis.components(interpolation_dofs, ind=range(len(interpolation_dofs))).T
        errs.append(err)

        logger.info('')

    if len(interpolation_dofs) < len(collateral_basis):
        collateral_basis.remove(ind=range(len(interpolation_dofs), len(collateral_basis)))

    logger.info('Finished.'.format(new_dof))

    data = {'errors': errs}

    return interpolation_dofs, collateral_basis, data


class EvaluationProvider(CacheableInterface):
    '''Helper class for providing cached operator evaluations that can be fed into :func:`ei_greedy`.

    This class calls `solve()` on a given |Discretization| for a provided sample of |Parameters| and
    then applies |Operators| to the solutions. The results are cached.

    Parameters
    ----------
    discretization
        The |Discretization| whose `solve()` method will be called.
    operators
        A list of |Operators| which are evaluated on the solution snapshots.
    sample
        A list of |Parameters| for which `discretization.solve()` is called.
    cache_region
        Name of the |CacheRegion| to use.
    '''

    def __init__(self, discretization, operators, sample, cache_region='memory'):
        self.cache_region = cache_region
        self.discretization = discretization
        self.sample = sample
        self.operators = operators

    @cached
    def data(self, k):
        mu = self.sample[k]
        U = self.discretization.solve(mu)
        AU = self.operators[0].type_range.empty(self.operators[0].dim_range,
                                                reserve=len(self.operators))
        for op in self.operators:
            AU.append(op.apply(U, mu=mu))
        return AU

    def __len__(self):
        return len(self.sample)

    def __getitem__(self, ind):
        if not 0 <= ind < len(self.sample):
            raise IndexError
        return self.data(ind)


def interpolate_operators(discretization, operator_names, parameter_sample, error_norm=None,
                          target_error=None, max_interpolation_dofs=None,
                          projection='orthogonal', product=None, cache_region='memory'):
    '''Empirical operator interpolation using the EI-Greedy algorithm.

    This is a convenience method for facilitating the use of :func:`ei_greedy`. Given
    a |Discretization|, names of |Operators|, and a sample of |Parameters|, first the operators
    are evaluated on the solution snapshots of the discretization for the provided parameters.
    These evaluations are then used as input for :func:`ei_greedy`. Finally the resulting
    interpolation data is used to create |EmpiricalInterpolatedOperators| and a new
    discretization with the interpolated operators is returned.

    Note that this implementation creates ONE common collateral basis for all operators
    which might not be what you want.

    Parameters
    ----------
    discretization
        The |Discretization| whose |Operators| will be interpolated.
    operator_names
        List of keys in the `operators` dict of the discretization. The corresponding
        |Operators| will be interpolated.
    sample
        A list of |Parameters| for which solution snapshots are calculated.
    error_norm
        See :func:`ei_greedy`.
    target_error
        See :func:`ei_greedy`.
    max_interpolation_dofs
        See :func:`ei_greedy`.
    projection
        See :func:`ei_greedy`.
    product
        See :func:`ei_greedy`.
    cache_region
        Name of the |CacheRegion| in which the operator evaluations will be stored.

    Returns
    -------
    ei_discretization
        |Discretization| with |Operators| given by `operator_names` replaced by
        |EmpiricalInterpolatedOperators|.
    data
        Dict containing the following fields:

            :dofs:   |NumPy array| of the DOFs at which the |Operators| have to be evaluated.
            :basis:  |VectorArray| containing the generated collateral basis.
            :errors: sequence of maximum approximation errors during greedy search.
    '''

    sample = tuple(parameter_sample)
    operators = [discretization.operators[operator_name] for operator_name in operator_names]

    evaluations = EvaluationProvider(discretization, operators, sample, cache_region=cache_region)
    dofs, basis, data = ei_greedy(evaluations, error_norm, target_error, max_interpolation_dofs,
                                  projection=projection, product=product)

    ei_operators = {name: EmpiricalInterpolatedOperator(operator, dofs, basis)
                    for name, operator in zip(operator_names, operators)}
    operators_dict = discretization.operators.copy()
    operators_dict.update(ei_operators)
    ei_discretization = discretization.with_(operators=operators_dict, name='{}_ei'.format(discretization.name))

    data.update({'dofs': dofs, 'basis': basis})
    return ei_discretization, data
