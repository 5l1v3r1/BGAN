#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
Example employing Lasagne for digit generation using the MNIST dataset and
Deep Convolutional Generative Adversarial Networks
(DCGANs, see http://arxiv.org/abs/1511.06434).
It is based on the MNIST example in Lasagne:
http://lasagne.readthedocs.org/en/latest/user/tutorial.html
Note: In contrast to the original paper, this trains the generator and
discriminator at once, not alternatingly. It's easy to change, though.
Jan Schlüter, 2015-12-16
"""
from __future__ import print_function

import sys
import os
import time

import numpy as np
import theano
import theano.tensor as T
import pylab as pl
import lasagne
import scipy.misc
import cPickle
import gzip
from lasagne.layers import (InputLayer, ReshapeLayer,
                            DenseLayer, batch_norm, GaussianNoiseLayer)
from lasagne.layers.dnn import Conv2DDNNLayer as Conv2DLayer
from lasagne.nonlinearities import LeakyRectify, sigmoid
floatX = theano.config.floatX

#Dataset loader
def load_dataset(source, mode):
    print("Reading MNIST, ", mode)
    with gzip.open(source, 'rb') as f:
        x = cPickle.load(f)

    if mode == 'train':
        data = np.float32(x[0][0])
    elif mode == 'valid':
        data = np.float32(x[1][0])
    elif mode == 'test':
        data = np.float32(x[2][0])
    else:
        raise ValueError()

    data = np.reshape(data, (-1, 1, 28, 28))
    return data

#Dataset iterator
def iterate_minibatches(inputs, batchsize, shuffle=False):
    if shuffle:
        indices = np.arange(len(inputs))
        np.random.shuffle(indices)
    for start_idx in range(0, len(inputs) - batchsize + 1, batchsize):
        if shuffle:
            excerpt = indices[start_idx:start_idx + batchsize]
        else:
            excerpt = slice(start_idx, start_idx + batchsize)
        yield inputs[excerpt]


# ##################### Build the neural network model #######################
# We create two models: The generator and the discriminator network. The
# generator needs a transposed convolution layer defined first.

#Transponsed/fractional-strided convolutional layer
class Deconv2DLayer(lasagne.layers.Layer):
    def __init__(self, incoming, num_filters, filter_size, stride=1, pad=0, W=None, b=None,
                 nonlinearity=lasagne.nonlinearities.rectify, **kwargs):
        super(Deconv2DLayer, self).__init__(incoming, **kwargs)
        self.num_filters = num_filters
        self.filter_size = lasagne.utils.as_tuple(filter_size, 2, int)
        self.stride = lasagne.utils.as_tuple(stride, 2, int)
        self.pad = lasagne.utils.as_tuple(pad, 2, int)
        if W is None:
            self.W = self.add_param(lasagne.init.Orthogonal(),
                                    (self.input_shape[1], num_filters) + self.filter_size,
                                    name='W')
        else:
            self.W = self.add_param(W,
                                    (self.input_shape[1], num_filters) + self.filter_size,
                                    name='W')
        if b is None:
            self.b = self.add_param(lasagne.init.Constant(0),
                                    (num_filters,),
                                    name='b')
        else:
            self.b = self.add_param(b,
                                    (num_filters,),
                                    name='b')
        if nonlinearity is None:
            nonlinearity = lasagne.nonlinearities.identity
        self.nonlinearity = nonlinearity

    def get_output_shape_for(self, input_shape):
        shape = tuple(i * s - 2 * p + f - 1
                      for i, s, p, f in zip(input_shape[2:],
                                            self.stride,
                                            self.pad,
                                            self.filter_size))
        return (input_shape[0], self.num_filters) + shape

    def get_output_for(self, input, **kwargs):
        op = T.nnet.abstract_conv.AbstractConv2d_gradInputs(
            imshp=self.output_shape,
            kshp=(self.input_shape[1], self.num_filters) + self.filter_size,
            subsample=self.stride, border_mode=self.pad)
        conved = op(self.W, input, self.output_shape[2:])
        if self.b is not None:
            conved += self.b.dimshuffle('x', 0, 'x', 'x')
        return self.nonlinearity(conved)


def build_generator(input_var=None):
    from lasagne.layers import InputLayer, ReshapeLayer, DenseLayer, batch_norm
    from lasagne.nonlinearities import sigmoid
    # input: 100dim
    layer = InputLayer(shape=(None, 100), input_var=input_var)
    # fully-connected layer
    layer = batch_norm(DenseLayer(layer, 1024))
    # project and reshape
    layer = batch_norm(DenseLayer(layer, 128*7*7))
    layer = ReshapeLayer(layer, ([0], 128, 7, 7))
    # two fractional-stride convolutions
    layer = batch_norm(Deconv2DLayer(layer, 64, 5, stride=2, pad=2))
    layer = Deconv2DLayer(layer, 1, 5, stride=2, pad=2,
                          nonlinearity=sigmoid)
    print ("Generator output:", layer.output_shape)
    return layer

def build_discriminator(input_var=None):
    from lasagne.layers import (InputLayer, Conv2DLayer, ReshapeLayer,
                                DenseLayer, batch_norm)
    from lasagne.layers.dnn import Conv2DDNNLayer as Conv2DLayer  # override
    from lasagne.nonlinearities import LeakyRectify, sigmoid
    lrelu = LeakyRectify(0.2)
    # input: (None, 1, 28, 28)
    layer = InputLayer(shape=(None, 1, 28, 28), input_var=input_var)
    # two convolutions
    layer = batch_norm(Conv2DLayer(layer, 64, 5, stride=2, pad=2, nonlinearity=lrelu))
    layer = batch_norm(Conv2DLayer(layer, 128, 5, stride=2, pad=2, nonlinearity=lrelu))
    # fully-connected layer
    layer = batch_norm(DenseLayer(layer, 1024, nonlinearity=lrelu))
    # output layer
    layer = DenseLayer(layer, 1, nonlinearity=None)
    print ("Discriminator output:", layer.output_shape)
    return layer


def log_sum_exp(x, axis=None):
    '''Numerically stable log( sum( exp(A) ) ).

    '''
    x_max = T.max(x, axis=axis, keepdims=True)
    y = T.log(T.sum(T.exp(x - x_max), axis=axis, keepdims=True)) + x_max
    y = T.sum(y, axis=axis)
    return y

def norm_exp(log_factor):
    '''Gets normalized weights.

    '''
    log_factor = log_factor - T.log(log_factor.shape[0]).astype(floatX)
    w_norm   = log_sum_exp(log_factor, axis=0)
    log_w    = log_factor - T.shape_padleft(w_norm)
    w_tilde  = T.exp(log_w)
    return w_tilde

def reweighted_loss(fake_out):
    log_d1 = -T.nnet.softplus(-fake_out)  # -D_cell.neg_log_prob(1., P=d)
    log_d0 = -(fake_out+T.nnet.softplus(-fake_out))  # -D_cell.neg_log_prob(0., P=d)
    log_w = log_d1 - log_d0

    # Find normalized weights.
    log_N = T.log(log_w.shape[0]).astype(floatX)
    log_Z_est = log_sum_exp(log_w - log_N, axis=0)
    log_w_tilde = log_w - T.shape_padleft(log_Z_est) - log_N

    cost = ((log_w - T.maximum(log_Z_est, -2)) ** 2).mean()

    return cost

# ############################## Main program ################################
# Everything else will be handled in our main program now. We could pull out
# more functions to better separate the code, but it wouldn't make it any
# easier to read.

def main(num_epochs=40, initial_eta=0.0001):
    # Load the dataset
    print("Loading data...")
    source = "/u/jacobath/cortex-data/basic/mnist.pkl.gz"
    X_train= load_dataset(source=source, mode="train")

    # Prepare Theano variables for inputs and targets
    noise_var = T.matrix('noise')
    input_var = T.tensor4('inputs')
    # Create neural network model
    print("Building model and compiling functions...")
    generator = build_generator(noise_var)
    discriminator = build_discriminator(input_var)

    # Create expression for passing real data through the discriminator
    real_out = lasagne.layers.get_output(discriminator)
    fake_out = lasagne.layers.get_output(discriminator,
                                         lasagne.layers.get_output(generator))


    #Create gen_loss
    # gen_loss = reweighted_loss(fake_out)
    generator_loss = reweighted_loss(fake_out) #0.5*(T.nnet.softplus(-fake_out)).mean()

    #Create disc_loss
    discriminator_loss = (T.nnet.softplus(-real_out)).mean() + (T.nnet.softplus(-fake_out)).mean() + fake_out.mean()

    #Get generator and discriminator params
    generator_params = lasagne.layers.get_all_params(generator, trainable=True)
    discriminator_params = lasagne.layers.get_all_params(discriminator, trainable=True)

    #Set optimizers and learning rates
    eta = theano.shared(lasagne.utils.floatX(initial_eta))
    updates = lasagne.updates.rmsprop(
        generator_loss, generator_params, learning_rate=eta)
    updates.update(lasagne.updates.rmsprop(
        discriminator_loss, discriminator_params, learning_rate=eta))

    # Compile a training function
    train_fn = theano.function([noise_var, input_var],
                               [(real_out > 0.).mean(),
                                (fake_out < 0.).mean(),
                                generator_loss],
                               updates=updates)

    # Compile another generating function
    gen_fn = theano.function([noise_var],
                             lasagne.layers.get_output(generator,
                                                       deterministic=True))

    # Finally, launch the training loop.
    print("Starting training...")
    # We iterate over epochs:
    for epoch in range(num_epochs):
        # In each epoch, we do a full pass over the training data:
        train_err = 0
        train_batches = 0
        start_time = time.time()
        for batch in iterate_minibatches(X_train, 128, shuffle=True):
            inputs = np.array(batch, dtype=np.float32)
            noise = lasagne.utils.floatX(np.random.rand(len(inputs), 100))
            train_err += np.array(train_fn(noise, inputs))
            train_batches += 1
        # Then we print the results for this epoch:
        print("Epoch {} of {} took {:.3f}s".format(
            epoch + 1, num_epochs, time.time() - start_time))
        print("  training loss:\t\t{}".format(train_err / train_batches))
        # And finally, we plot some generated data
        prefix = "%d_" % epoch
        if epoch % 1 == 0:
            samples = gen_fn(lasagne.utils.floatX(np.random.rand(100, 100)))
            import matplotlib.pyplot as plt
            plt.imsave('./gen_images/' + prefix + '.png',
                       (samples.reshape(10, 10, 28, 28)
                        .transpose(0, 2, 1, 3)
                        .reshape(10 * 28, 10 * 28)),
                       cmap='gray')



if __name__ == '__main__':
    if ('--help' in sys.argv) or ('-h' in sys.argv):
        print("Trains a DCGAN on MNIST using Lasagne.")
        print("Usage: %s [EPOCHS]" % sys.argv[0])
        print()
        print("EPOCHS: number of training epochs to perform (default: 100)")
    else:
        main()