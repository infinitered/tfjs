/**
 * @license
 * Copyright 2017 Google Inc. All Rights Reserved.
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 * http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 * =============================================================================
 */

import {ENV} from '../../environment';
import {keep, tidy} from '../../globals';
import {Node} from '../../graph/graph';
import {SessionRuntime} from '../../graph/session';
// tslint:disable-next-line:max-line-length
import {SummedTensorArrayMap, TensorArrayMap} from '../../graph/tensor_array_map';
import {NDArrayMath} from '../../math/math';
import {doc} from '../decorators';
import * as ops from '../ops';
import {Scalar} from '../tensor';
import {NamedTensorMap} from '../types';

import {Optimizer} from './optimizer';

/**
 * Optimizer that implements stochastic gradient descent.
 *
 * Use `dl.train.sgd` to create an SGD optimizer.
 */
@doc({heading: 'Training', subheading: 'Optimizers', namespace: 'train'})
export class SGDOptimizer extends Optimizer {
  protected c: Scalar;

  constructor(protected learningRate: number, specifiedVariableList?: Node[]) {
    super(learningRate, specifiedVariableList);
    this.setLearningRate(learningRate);
  }

  // Eager mode
  applyGradients(variableGradients: NamedTensorMap) {
    const varNames = Object.keys(variableGradients);
    varNames.forEach(varName => {
      const gradient = variableGradients[varName];
      const value = ENV.engine.registeredVariables[varName];

      const newValue = tidy(() => this.c.mul(gradient).add(value));

      value.assign(newValue);
    });
  }

  // Graph
  afterBatch(
      math: NDArrayMath, batchSize: number, runtime: SessionRuntime,
      activationArrayMap: TensorArrayMap,
      gradientArrayMap: SummedTensorArrayMap) {
    tidy(() => {
      this.variableNodes.forEach(node => {
        const oldVariable = activationArrayMap.get(node.output);
        const gradient = this.variableGradients.get(node.output);
        const variable =
            math.scaledArrayAdd(this.cGraph, gradient, this.one, oldVariable);
        activationArrayMap.set(node.output, keep(variable));
        node.data = variable;

        oldVariable.dispose();
      });
    });

    this.variableGradients.dispose();
    this.variableGradients = new TensorArrayMap();
  }

  dispose() {
    this.c.dispose();
    super.dispose();
  }

  setLearningRate(learningRate: number) {
    this.learningRate = learningRate;
    if (this.c != null) {
      this.c.dispose();
    }
    this.c = keep(ops.scalar(-learningRate));
  }
}