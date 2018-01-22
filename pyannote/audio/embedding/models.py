#!/usr/bin/env python
# encoding: utf-8

# The MIT License (MIT)

# Copyright (c) 2017-2018 CNRS

# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.

# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

# AUTHORS
# Hervé BREDIN - http://herve.niderb.fr


import torch
from torch.autograd import Variable
import torch.nn as nn
import torch.nn.functional as F


class ClopiNet(nn.Module):
    """ClopiNet sequence embedding

    RNN          ⎤
      » RNN      ⎥ » MLP » Weight » temporal pooling › normalize
           » RNN ⎦

    Parameters
    ----------
    n_features : int
        Input feature dimension.
    rnn : {'LSTM', 'GRU'}, optional
        Defaults to 'LSTM'.
    recurrent : list, optional
        List of hidden dimensions of stacked recurrent layers. Defaults to
        [16, ], i.e. one recurrent layer with hidden dimension of 16.
    bidirectional : bool, optional
        Use bidirectional recurrent layers. Defaults to False, i.e. use
        mono-directional RNNs.
    linear : list, optional
        List of hidden dimensions of linear layers. Defaults to [16, ], i.e.
        one linear layer with hidden dimension of 16.
    weighted : bool, optional
        Add dimension-wise trainable weights. Defaults to False.
    internal : bool, optional
        Return sequence of internal embeddings. Defaults to False.
    normalize : bool, optional
        Set to False to **not** unit-normalize embeddings.
    attention : list of int, optional
        List of hidden dimensions of attention linear layers (e.g. [16, ]).
        Defaults to False (i.e. no attention).
    return_attention : bool, optional
    batch_normalization : bool, optional
        Defaults to False. Has not effect when internal is set to True.

    Usage
    -----
    >>> model = ClopiNet(n_features)
    >>> embedding = model(sequence)
    """

    def __init__(self, n_features,
                 rnn='LSTM', recurrent=[16,], bidirectional=False,
                 linear=[16, ], weighted=False, internal=False,
                 normalize=True, attention=False, return_attention=False,
                 batch_normalization=False):

        super(ClopiNet, self).__init__()

        self.n_features = n_features
        self.rnn = rnn
        self.recurrent = recurrent
        self.bidirectional = bidirectional
        self.linear = linear
        self.weighted = weighted
        self.internal = internal
        self.normalize = normalize
        self.attention = attention
        self.return_attention = return_attention
        self.batch_normalization = batch_normalization

        self.num_directions_ = 2 if self.bidirectional else 1

        # create list of recurrent layers
        self.recurrent_layers_ = []
        input_dim = self.n_features
        for i, hidden_dim in enumerate(self.recurrent):
            if self.rnn == 'LSTM':
                recurrent_layer = nn.LSTM(input_dim, hidden_dim,
                                          bidirectional=self.bidirectional)
            elif self.rnn == 'GRU':
                recurrent_layer = nn.GRU(input_dim, hidden_dim,
                                         bidirectional=self.bidirectional)
            else:
                raise ValueError('"rnn" must be one of {"LSTM", "GRU"}.')
            self.add_module('recurrent_{0}'.format(i), recurrent_layer)
            self.recurrent_layers_.append(recurrent_layer)
            input_dim = hidden_dim

        # the output of recurrent layers are concatenated so the input
        # dimension of subsequent linear layers is the sum of their output
        # dimension
        input_dim = sum(self.recurrent)

        # create list of linear layers
        self.linear_layers_ = []
        for i, hidden_dim in enumerate(self.linear):
            linear_layer = nn.Linear(input_dim, hidden_dim, bias=True)
            self.add_module('linear_{0}'.format(i), linear_layer)
            self.linear_layers_.append(linear_layer)
            input_dim = hidden_dim

        if self.weighted:
            self.alphas_ = nn.Parameter(torch.ones(input_dim))

        if self.batch_normalization:
            self.batch_norm_ = nn.BatchNorm1d(input_dim, eps=1e-5,
                                              momentum=0.1, affine=False)

        # create attention layers
        self.attention_layers_ = []
        if not self.attention:
            return

        input_dim = self.n_features
        for i, hidden_dim in enumerate(self.attention):
            attention_layer = nn.Linear(input_dim, hidden_dim, bias=True)
            self.add_module('attention_{0}'.format(i), attention_layer)
            self.attention_layers_.append(attention_layer)
            input_dim = hidden_dim
        if input_dim > 1:
            attention_layer = nn.Linear(input_dim, 1, bias=True)
            self.add_module('attention_{0}'.format(len(self.attention)),
                            attention_layer)
            self.attention_layers_.append(attention_layer)

    @property
    def output_dim(self):
        if self.linear:
            return self.linear[-1]
        return sum(self.recurrent)

    def forward(self, sequence):

        # check input feature dimension
        n_samples, batch_size, n_features = sequence.size()
        if n_features != self.n_features:
            msg = 'Wrong feature dimension. Found {0}, should be {1}'
            raise ValueError(msg.format(n_features, self.n_features))

        output = sequence

        gpu = sequence.is_cuda

        outputs = []
        # stack recurrent layers
        for hidden_dim, layer in zip(self.recurrent, self.recurrent_layers_):

            if self.rnn == 'LSTM':

                # initial hidden and cell states
                h = torch.zeros(self.num_directions_, batch_size, hidden_dim)
                c = torch.zeros(self.num_directions_, batch_size, hidden_dim)
                if gpu:
                    h = h.cuda()
                    c = c.cuda()
                hidden = (Variable(h, requires_grad=False),
                          Variable(c, requires_grad=False))

            elif self.rnn == 'GRU':
                # initial hidden state
                h = torch.zeros(self.num_directions_, batch_size, hidden_dim)
                if gpu:
                    h = h.cuda()
                hidden = Variable(h, requires_grad=False)

            # apply current recurrent layer and get output sequence
            output, _ = layer(output, hidden)

            # average both directions in case of bidirectional layers
            if self.bidirectional:
                output = .5 * (output[:, :, :hidden_dim] + \
                               output[:, :, hidden_dim:])

            outputs.append(output)

        # concatenate outputs
        output = torch.cat(outputs, dim=2)
        # n_samples, batch_size, dimension

        # stack linear layers
        for hidden_dim, layer in zip(self.linear, self.linear_layers_):

            # apply current linear layer
            output = layer(output)

            # apply non-linear activation function
            output = F.tanh(output)

        # n_samples, batch_size, dimension

        if self.weighted:
            if gpu:
                self.alphas_ = self.alphas_.cuda()
            output = output * self.alphas_

        if self.internal:
            if self.normalize:
                output = output / torch.norm(output, 2, 2, keepdim=True)

            # batch normalization
            return output

        if self.attention_layers_:
            attn = sequence
            for layer, hidden_dim in zip(self.attention_layers_,
                                         self.attention + [1]):
                attn = layer(attn)
                attn = F.tanh(attn)
            attn = F.softmax(attn, dim=0)
            output = output * attn

        # average temporal pooling
        output = output.sum(dim=0)
        # batch_size, dimension

        # L2 normalization
        if self.normalize:
            output = output / torch.norm(output, 2, 1, keepdim=True)
            # batch_size, dimension

        # batch normalization
        if self.batch_normalization:
            output = self.batch_norm_(output)

        if self.return_attention:
            return output, attn

        return output
