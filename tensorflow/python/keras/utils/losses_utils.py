# Copyright 2018 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
# pylint: disable=protected-access
"""Utilities related to loss functions."""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from tensorflow.python.distribute import distribution_strategy_context
from tensorflow.python.framework import ops
from tensorflow.python.keras import backend as K
from tensorflow.python.keras.engine import keras_tensor
from tensorflow.python.ops import array_ops
from tensorflow.python.ops import control_flow_ops
from tensorflow.python.ops import math_ops
from tensorflow.python.ops.losses import loss_reduction
from tensorflow.python.ops.losses import util as tf_losses_utils
from tensorflow.python.util.tf_export import keras_export


# TODO(joshl/psv): Update references to ReductionV2 to point to its
# new location.
ReductionV2 = keras_export(  # pylint: disable=invalid-name
    'keras.losses.Reduction', v1=[])(loss_reduction.ReductionV2)


def remove_squeezable_dimensions(
    labels, predictions, expected_rank_diff=0, name=None):
  """Squeeze last dim if ranks differ from expected by exactly 1.

  In the common case where we expect shapes to match, `expected_rank_diff`
  defaults to 0, and we squeeze the last dimension of the larger rank if they
  differ by 1.

  But, for example, if `labels` contains class IDs and `predictions` contains 1
  probability per class, we expect `predictions` to have 1 more dimension than
  `labels`, so `expected_rank_diff` would be 1. In this case, we'd squeeze
  `labels` if `rank(predictions) - rank(labels) == 0`, and
  `predictions` if `rank(predictions) - rank(labels) == 2`.

  This will use static shape if available. Otherwise, it will add graph
  operations, which could result in a performance hit.

  Args:
    labels: Label values, a `Tensor` whose dimensions match `predictions`.
    predictions: Predicted values, a `Tensor` of arbitrary dimensions.
    expected_rank_diff: Expected result of `rank(predictions) - rank(labels)`.
    name: Name of the op.

  Returns:
    Tuple of `labels` and `predictions`, possibly with last dim squeezed.
  """
  with ops.name_scope(name, 'remove_squeezable_dimensions',
                      [labels, predictions]):
    predictions = ops.convert_to_tensor_v2_with_dispatch(predictions)
    labels = ops.convert_to_tensor_v2_with_dispatch(labels)
    predictions_shape = predictions.get_shape()
    predictions_rank = predictions_shape.ndims
    labels_shape = labels.get_shape()
    labels_rank = labels_shape.ndims
    if (labels_rank is not None) and (predictions_rank is not None):
      # Use static rank.
      rank_diff = predictions_rank - labels_rank
      if (rank_diff == expected_rank_diff + 1 and
          predictions_shape.dims[-1].is_compatible_with(1)):
        predictions = array_ops.squeeze(predictions, [-1])
      elif (rank_diff == expected_rank_diff - 1 and
            labels_shape.dims[-1].is_compatible_with(1)):
        labels = array_ops.squeeze(labels, [-1])
      return labels, predictions

    # Use dynamic rank.
    rank_diff = array_ops.rank(predictions) - array_ops.rank(labels)
    if (predictions_rank is None) or (
        predictions_shape.dims[-1].is_compatible_with(1)):
      predictions = control_flow_ops.cond(
          math_ops.equal(expected_rank_diff + 1, rank_diff),
          lambda: array_ops.squeeze(predictions, [-1]),
          lambda: predictions)
    if (labels_rank is None) or (
        labels_shape.dims[-1].is_compatible_with(1)):
      labels = control_flow_ops.cond(
          math_ops.equal(expected_rank_diff - 1, rank_diff),
          lambda: array_ops.squeeze(labels, [-1]),
          lambda: labels)
    return labels, predictions


def squeeze_or_expand_dimensions(y_pred, y_true=None, sample_weight=None):
  """Squeeze or expand last dimension if needed.

  1. Squeezes last dim of `y_pred` or `y_true` if their rank differs by 1
  (using `remove_squeezable_dimensions`).
  2. Squeezes or expands last dim of `sample_weight` if its rank differs by 1
  from the new rank of `y_pred`.
  If `sample_weight` is scalar, it is kept scalar.

  This will use static shape if available. Otherwise, it will add graph
  operations, which could result in a performance hit.

  Args:
    y_pred: Predicted values, a `Tensor` of arbitrary dimensions.
    y_true: Optional label `Tensor` whose dimensions match `y_pred`.
    sample_weight: Optional weight scalar or `Tensor` whose dimensions match
      `y_pred`.

  Returns:
    Tuple of `y_pred`, `y_true` and `sample_weight`. Each of them possibly has
    the last dimension squeezed,
    `sample_weight` could be extended by one dimension.
    If `sample_weight` is None, (y_pred, y_true) is returned.
  """
  y_pred_shape = y_pred.shape
  y_pred_rank = y_pred_shape.ndims
  if y_true is not None:

    # If sparse matrix is provided as `y_true`, the last dimension in `y_pred`
    # may be > 1. Eg: y_true = [0, 1, 2] (shape=(3,)),
    # y_pred = [[.9, .05, .05], [.5, .89, .6], [.05, .01, .94]] (shape=(3, 3))
    # In this case, we should not try to remove squeezable dimension.
    y_true_shape = y_true.shape
    y_true_rank = y_true_shape.ndims
    if (y_true_rank is not None) and (y_pred_rank is not None):
      # Use static rank for `y_true` and `y_pred`.
      if (y_pred_rank - y_true_rank != 1) or y_pred_shape[-1] == 1:
        y_true, y_pred = remove_squeezable_dimensions(
            y_true, y_pred)
    else:
      # Use dynamic rank.
      rank_diff = array_ops.rank(y_pred) - array_ops.rank(y_true)
      squeeze_dims = lambda: remove_squeezable_dimensions(  # pylint: disable=g-long-lambda
          y_true, y_pred)
      is_last_dim_1 = math_ops.equal(1, array_ops.shape(y_pred)[-1])
      maybe_squeeze_dims = lambda: control_flow_ops.cond(  # pylint: disable=g-long-lambda
          is_last_dim_1, squeeze_dims, lambda: (y_true, y_pred))
      y_true, y_pred = control_flow_ops.cond(
          math_ops.equal(1, rank_diff), maybe_squeeze_dims, squeeze_dims)

  if sample_weight is None:
    return y_pred, y_true

  weights_shape = sample_weight.shape
  weights_rank = weights_shape.ndims
  if weights_rank == 0:  # If weights is scalar, do nothing.
    return y_pred, y_true, sample_weight

  if (y_pred_rank is not None) and (weights_rank is not None):
    # Use static rank.
    if weights_rank - y_pred_rank == 1:
      sample_weight = array_ops.squeeze(sample_weight, [-1])
    elif y_pred_rank - weights_rank == 1:
      sample_weight = array_ops.expand_dims(sample_weight, [-1])
    return y_pred, y_true, sample_weight

  # Use dynamic rank.
  weights_rank_tensor = array_ops.rank(sample_weight)
  rank_diff = weights_rank_tensor - array_ops.rank(y_pred)
  maybe_squeeze_weights = lambda: array_ops.squeeze(sample_weight, [-1])

  def _maybe_expand_weights():
    expand_weights = lambda: array_ops.expand_dims(sample_weight, [-1])
    return control_flow_ops.cond(
        math_ops.equal(rank_diff, -1), expand_weights, lambda: sample_weight)

  def _maybe_adjust_weights():
    return control_flow_ops.cond(
        math_ops.equal(rank_diff, 1), maybe_squeeze_weights,
        _maybe_expand_weights)

  # squeeze or expand last dim of `sample_weight` if its rank differs by 1
  # from the new rank of `y_pred`.
  sample_weight = control_flow_ops.cond(
      math_ops.equal(weights_rank_tensor, 0), lambda: sample_weight,
      _maybe_adjust_weights)
  return y_pred, y_true, sample_weight


def _safe_mean(losses, num_present):
  """Computes a safe mean of the losses.

  Args:
    losses: `Tensor` whose elements contain individual loss measurements.
    num_present: The number of measurable elements in `losses`.

  Returns:
    A scalar representing the mean of `losses`. If `num_present` is zero,
      then zero is returned.
  """
  total_loss = math_ops.reduce_sum(losses)
  return math_ops.div_no_nan(total_loss, num_present, name='value')


def _num_elements(losses):
  """Computes the number of elements in `losses` tensor."""
  with K.name_scope('num_elements') as scope:
    return math_ops.cast(array_ops.size(losses, name=scope), dtype=losses.dtype)


def reduce_weighted_loss(weighted_losses,
                         reduction=ReductionV2.SUM_OVER_BATCH_SIZE):
  """Reduces the individual weighted loss measurements."""
  if reduction == ReductionV2.NONE:
    loss = weighted_losses
  else:
    loss = math_ops.reduce_sum(weighted_losses)
    if reduction == ReductionV2.SUM_OVER_BATCH_SIZE:
      loss = _safe_mean(loss, _num_elements(weighted_losses))
  return loss


def compute_weighted_loss(losses,
                          sample_weight=None,
                          reduction=ReductionV2.SUM_OVER_BATCH_SIZE,
                          name=None):
  """Computes the weighted loss.

  Args:
    losses: `Tensor` of shape `[batch_size, d1, ... dN]`.
    sample_weight: Optional `Tensor` whose rank is either 0, or the same rank as
      `losses`, or be broadcastable to `losses`.
    reduction: (Optional) Type of `tf.keras.losses.Reduction` to apply to loss.
      Default value is `SUM_OVER_BATCH_SIZE`.
    name: Optional name for the op.

  Raises:
    ValueError: If the shape of `sample_weight` is not compatible with `losses`.

  Returns:
    Weighted loss `Tensor` of the same type as `losses`. If `reduction` is
    `NONE`, this has the same shape as `losses`; otherwise, it is scalar.
  """
  ReductionV2.validate(reduction)

  # If this function is called directly, then we just default 'AUTO' to
  # 'SUM_OVER_BATCH_SIZE'. Eg. Canned estimator use cases.
  if reduction == ReductionV2.AUTO:
    reduction = ReductionV2.SUM_OVER_BATCH_SIZE
  if sample_weight is None:
    sample_weight = 1.0
  with K.name_scope(name or 'weighted_loss'):
    # Save the `reduction` argument for loss normalization when distributing
    # to multiple replicas. Used only for estimator + v1 optimizer flow.
    ops.get_default_graph()._last_loss_reduction = reduction  # pylint: disable=protected-access

    if not isinstance(losses, keras_tensor.KerasTensor):
      losses = ops.convert_to_tensor_v2(losses)
    input_dtype = losses.dtype

    if not isinstance(sample_weight, keras_tensor.KerasTensor):
      sample_weight = ops.convert_to_tensor_v2(sample_weight)
    weighted_losses = tf_losses_utils.scale_losses_by_sample_weight(
        losses, sample_weight)
    # Apply reduction function to the individual weighted losses.
    loss = reduce_weighted_loss(weighted_losses, reduction)
    # Convert the result back to the input type.
    loss = math_ops.cast(loss, input_dtype)
    return loss


def scale_loss_for_distribution(loss_value):
  """Scales and returns the given loss value by the number of replicas."""
  num_replicas = (
      distribution_strategy_context.get_strategy().num_replicas_in_sync)
  if num_replicas > 1:
    loss_value *= (1. / num_replicas)
  return loss_value


def cast_losses_to_common_dtype(losses):
  """Cast a list of losses to a common dtype.

  If any loss is floating-point, they will all be casted to the most-precise
  floating-point loss. Otherwise the losses are not casted. We also skip casting
  losses if there are any complex losses.

  Args:
    losses: A list of losses.

  Returns:
    `losses`, but they have been casted to a common dtype.
  """
  highest_float = None
  for loss in losses:
    if loss.dtype.is_floating:
      if highest_float is None or loss.dtype.size > highest_float.size:
        highest_float = loss.dtype
      elif {loss.dtype, highest_float} == {'bfloat16', 'float16'}:
        highest_float = 'float32'
    if loss.dtype.is_complex:
      return losses  # If we find any complex losses, do not cast any losses
  if highest_float:
    losses = [math_ops.cast(loss, highest_float) for loss in losses]
  return losses
