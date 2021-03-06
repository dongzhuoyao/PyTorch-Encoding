##+++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
## Created by: Hang Zhang
## ECE Department, Rutgers University
## Email: zhang.hang@rutgers.edu
## Copyright (c) 2017
##
## This source code is licensed under the MIT-style license found in the
## LICENSE file in the root directory of this source tree
##+++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++

"""Synchronized Cross-GPU Batch Normalization Module"""
import threading
import torch
from torch.nn import Module, Sequential, Conv1d, Conv2d, ConvTranspose2d, \
    ReLU, Sigmoid, MaxPool2d, AvgPool2d, AdaptiveAvgPool2d, Dropout2d, Linear
from torch.nn.modules.batchnorm import _BatchNorm

from ..functions import batchnormtrain, batchnormeval, sum_square
from ..parallel import allreduce

__all__ = ['BatchNorm1d', 'BatchNorm2d', 'BatchNorm3d', 'Module', 'Sequential', 'Conv1d',
           'Conv2d', 'ConvTranspose2d', 'ReLU', 'Sigmoid', 'MaxPool2d', 'AvgPool2d',
           'AdaptiveAvgPool2d', 'Dropout2d', 'Linear']

class _SyncBatchNorm(_BatchNorm):
    # pylint: disable=access-member-before-definition
    def __init__(self, num_features, eps=1e-5, momentum=0.1, **kwargs):
        super(_SyncBatchNorm, self).__init__(num_features, eps=1e-5, momentum=0.1, **kwargs)
        # syncBN
        self.writelock = threading.Lock()
        nGPUs = torch.cuda.device_count()
        self.sharedT = SharedTensor(nGPUs)

    def forward(self, input):
        self._check_input_dim(input)
        input_shape = input.size()
        input = input.view(input_shape[0], self.num_features, -1)
        if not self.training:
            std = (self.running_var.clamp(self.eps)).sqrt()
            output = batchnormeval(input, self.weight, self.bias, self.running_mean, std)
            return output.view(input_shape)
        # get global sum(x) and sum(x^2)
        xsum, xsquare = self.sharedT(sum_square(input.unsqueeze(3)))
        # calculate mean, var
        N = len(self.sharedT) * input.size(0) * input.size(2)
        mean = xsum / N
        sumvar = xsquare - xsum * xsum / N
        unbias_var = sumvar / (N - 1)
        bias_var = sumvar / N
        std = bias_var.clamp(self.eps).sqrt()
        # update running_mean and var
        self.running_mean = (1-self.momentum) * self.running_mean + self.momentum * mean.data
        self.running_var = (1-self.momentum) * self.running_var + self.momentum * unbias_var.data
        # forward
        return batchnormtrain(input, self.weight, self.bias, mean, std).view(input_shape)


class BatchNorm1d(_SyncBatchNorm):
    r"""Please see the docs in :class:`encoding.nn.BatchNorm2d`"""
    def _check_input_dim(self, input):
        if input.dim() != 2 and input.dim() != 3:
            raise ValueError('expected 2D or 3D input (got {}D input)'
                             .format(input.dim()))

class BatchNorm2d(_SyncBatchNorm):
    r"""Cross-GPU Synchronized Batch normalization (SyncBN)

    Standard BN [1]_ implementation only normalize the data within each device.
    SyncBN normalizes the input within the whole mini-batch.
    We follow the sync-onece implmentation described in the paper [2]_ .
    Please see the design idea in the `notes <./notes/syncbn.html>`_.

    .. math::

        y = \frac{x - mean[x]}{ \sqrt{Var[x] + \epsilon}} * gamma + beta

    The mean and standard-deviation are calculated per-channel over
    the mini-batches and gamma and beta are learnable parameter vectors
    of size C (where C is the input size).

    During training, this layer keeps a running estimate of its computed mean
    and variance. The running sum is kept with a default momentum of 0.1.

    During evaluation, this running mean/variance is used for normalization.

    Because the BatchNorm is done over the `C` dimension, computing statistics
    on `(N, H, W)` slices, it's common terminology to call this Spatial BatchNorm

    Args:
        num_features: num_features from an expected input of
            size batch_size x num_features x height x width
        eps: a value added to the denominator for numerical stability.
            Default: 1e-5
        momentum: the value used for the running_mean and running_var
            computation. Default: 0.1
        affine: a boolean value that when set to ``True``, gives the layer learnable
            affine parameters. Default: ``True``

    Shape:
        - Input: :math:`(N, C, H, W)`
        - Output: :math:`(N, C, H, W)` (same shape as input)

    Reference:
        .. [1] Ioffe, Sergey, and Christian Szegedy. "Batch normalization: Accelerating deep network training by reducing internal covariate shift." *ICML 2015*
        .. [2] Hang Zhang, Kristin Dana, Jianping Shi, Zhongyue Zhang, Xiaogang Wang, Ambrish Tyagi, and Amit Agrawal. "Context Encoding for Semantic Segmentation." *CVPR 2018*

    Examples:
        >>> # Use exactly the same as standard BatchNrom2d
        >>> m = nn.BatchNorm2d(100)
        >>> output = m(input)
    """
    def _check_input_dim(self, input):
        if input.dim() != 4:
            raise ValueError('expected 4D input (got {}D input)'
                             .format(input.dim()))

class BatchNorm3d(_SyncBatchNorm):
    r"""Please see the docs in :class:`encoding.nn.BatchNorm2d`"""
    def _check_input_dim(self, input):
        if input.dim() != 5:
            raise ValueError('expected 5D input (got {}D input)'
                             .format(input.dim()))

class SharedTensor(object):
    """Shared Tensor for cross GPU communication
    """
    def __init__(self, nGPUs):
        self.mutex = threading.Lock()
        self.all_tasks_done = threading.Condition(self.mutex)
        self.nGPUs = nGPUs
        self._clear()

    def _clear(self):
        self.list = []
        self.push_tasks = self.nGPUs
        self.reduce_tasks = self.nGPUs

    def __call__(self, *inputs):
        # push from device
        with self.mutex:
            if self.push_tasks == 0:
                self._clear()
            self.list.extend(list(*inputs))
            idx = self.nGPUs - self.push_tasks
            self.push_tasks -= 1
        with self.all_tasks_done:
            if self.push_tasks == 0:
                self.all_tasks_done.notify_all()
            while self.push_tasks:
                self.all_tasks_done.wait()
        # pull from device
        with self.mutex:
            if self.reduce_tasks == self.nGPUs:
                assert(len(self.list) == 2 * self.nGPUs)
                self.list = allreduce(2, *self.list)
                self.reduce_tasks -= 1
            else:
                self.reduce_tasks -= 1
        with self.all_tasks_done:
            if self.reduce_tasks == 0:
                self.all_tasks_done.notify_all()
            while self.reduce_tasks:
                self.all_tasks_done.wait()
        # all reduce done
        return self.list[2*idx], self.list[2*idx+1]

    def __len__(self):
        return self.nGPUs

    def __repr__(self):
        return ('SharedTensor')
