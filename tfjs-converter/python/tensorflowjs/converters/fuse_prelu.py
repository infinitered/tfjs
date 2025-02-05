# Copyright 2019 Google LLC
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
"""
 This transformation rule tries to identify the PRelu structure generated by
 Keras, and convert it to a single op.
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import tensorflow as tf

from tensorflow.core.framework import attr_value_pb2
from tensorflow.core.framework import graph_pb2
from tensorflow.core.framework import node_def_pb2
from tensorflow.core.framework import op_def_pb2
from tensorflow.core.framework import types_pb2
from tensorflow.python.framework import function
from tensorflow.python.framework import op_def_registry

from tensorflowjs.converters import common

def register_prelu_op():
  """global registry of PReLU op for python, this allow metagraph to be
  properly generated with unregistered Prelu op
  """

  value = attr_value_pb2.AttrValue()
  value.list.type.extend([types_pb2.DT_FLOAT])
  attr = op_def_pb2.OpDef.AttrDef()
  attr.name = 'T'
  attr.type = 'type'
  attr.allowed_values.CopyFrom(value)
  prelu_op_def = op_def_pb2.OpDef()
  prelu_op_def.name = 'Prelu'
  prelu_op_def.attr.extend([attr])
  missing_op_list = op_def_pb2.OpList()
  missing_op_list.op.extend([prelu_op_def])
  op_def_registry.register_op_list(missing_op_list)

def fuse_ops_for_prelu(input_graph_def):
  """Modifies the provided graph by fusing a set of ops into a single Prelu op.
  The formula of PReLU is:
  f(x) = alpha * x for x < 0, f(x) = x for x >= 0.

  `x` is the input, and `alpha` is a trainable tensor which can be broadcasted
  to the shape of `x`.

  There's no native PRelu op in TensorFlow, so Keras generates the following
  structure which does the equivalent calculation:
  f(x) = Relu(x) + (-alpha * Relu(-x))

  Practically, alpha is always a constant in the inference graph, and grappler
  can have other graph transformations which fold the activation functions to
  other ops. Therefore, we're looking for the structure:

  f(x) = Relu(x) + (negative_alpha * Neg(x, activation=Relu))

  Args:
    input_graph_def: A GraphDef containing a model.

  Returns:
    Modified graph with Prelu ops generated, and modified weights.

  Raises:
    ValueError: If the graph is badly formed with duplicate node names.
  """
  input_node_map = {}
  for node in input_graph_def.node:
    if node.name not in input_node_map:
      input_node_map[node.name] = node
    else:
      raise ValueError("Duplicate node names detected for ", node.name)

  nodes_to_skip = {}
  for node in input_graph_def.node:
    if (node.op not in ("Add", "AddV2") or len(node.input) != 2):
      continue

    relu_input_op = common.node_from_map(input_node_map, node.input[0])
    if (not relu_input_op or relu_input_op.op != "Relu" or
        len(relu_input_op.input) != 1):
      continue

    mul_op = common.node_from_map(input_node_map, node.input[1])
    if (not mul_op or mul_op.op != 'Mul' or len(mul_op.input) != 2):
      continue

    neg_alpha_op = common.node_from_map(input_node_map, mul_op.input[0])
    if (not neg_alpha_op or len(neg_alpha_op.input) != 1):
      continue
    alpha_tensor_name = neg_alpha_op.input[0]

    relu_neg_input_op = common.node_from_map(input_node_map, mul_op.input[1])
    if (not relu_neg_input_op or len(relu_neg_input_op.input) != 1 or
        relu_neg_input_op.op != 'Relu'):
      continue

    # This detects a Neg op followed by a separated Relu op.
    neg_input_op = common.node_from_map(input_node_map,
                                        relu_neg_input_op.input[0])
    if (not neg_input_op or len(neg_input_op.input) != 1 or
        neg_input_op.op != 'Neg'):
      continue
    final_input_op = neg_input_op

    if relu_input_op.input[0] != final_input_op.input[0]:
      continue

    relu_input_op.op = 'Prelu'
    relu_input_op.input.extend([alpha_tensor_name])

    node.op = 'Identity'
    del node.input[:]
    node.input.append(relu_input_op.name)

    nodes_to_skip[mul_op.name] = True
    nodes_to_skip[relu_neg_input_op.name] = True
    nodes_to_skip[neg_input_op.name] = True

  result_graph_def = graph_pb2.GraphDef()
  for node in input_graph_def.node:
    if node.name in nodes_to_skip:
      continue
    new_node = node_def_pb2.NodeDef()
    new_node.CopyFrom(node)
    result_graph_def.node.extend([new_node])

  return result_graph_def

def fuse_prelu_with_fused_conv2d(input_graph_def):
  """Tensorflow does not support Prelu op, and the grappler remap optimizer
  will not fuse the prelu op with _FusedConv2D op. This method searches for
  the pattern and fuse the (_FusedConv2D + Prelu) nodes into a single
  _FusedConv2D op with activation information.

  Args:
    input_graph_def: A GraphDef containing a model.

  Returns:
    Modified graph with Prelu ops fused with _FusedConv2D as activation function

  Raises:
    ValueError: If the graph is badly formed with duplicate node names.
  """
  input_node_map = {}
  for node in input_graph_def.node:
    if node.name not in input_node_map:
      input_node_map[node.name] = node
    else:
      raise ValueError("Duplicate node names detected for ", node.name)

  for node in input_graph_def.node:
    if (node.op != "Prelu" or len(node.input) != 2):
      continue

    fused_conv_op = common.node_from_map(input_node_map, node.input[0])
    if (not fused_conv_op or fused_conv_op.op != "_FusedConv2D" or
        len(fused_conv_op.attr['fused_ops'].list.s) > 1):
      continue

    alpha_tensor_name = node.input[1]

    fused_conv_op.input.extend([alpha_tensor_name])
    fused_conv_op.attr['fused_ops'].list.s.extend([b'Prelu'])
    fused_conv_op.attr['num_args'].i = fused_conv_op.attr['num_args'].i + 1
    node.op = 'Identity'
    node.input[:] = [node.input[0]]

  return input_graph_def

def register_prelu_func(graph):
  """Register Prelu op with function def, this is need for importing graph_def
  with unregistered Prelu op.
  Args:
    graph: A tf.Graph object to insert prelu function into.
  """

  # Create a function for Prelu op
  @function.Defun(tf.float32, tf.float32, func_name='Prelu')
  def prelu_fn(*args):
    return tf.add(args[0], args[1])
  # Insert the function into graph
  with graph.as_default():
    prelu_fn(tf.constant(1.0), tf.constant(1.0))

register_prelu_op()
