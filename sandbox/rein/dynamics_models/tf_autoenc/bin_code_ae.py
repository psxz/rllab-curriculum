import numpy as np
import matplotlib.pyplot as plt

from keras.layers import Input, Dense, Lambda, Flatten, Reshape
from keras.layers import Convolution2D, Deconvolution2D
from keras.models import Model
from keras import backend as K
from keras import objectives
from keras.datasets import mnist

# input image dimensions
img_rows, img_cols, img_chns = 42, 42, 1
# number of convolutional filters to use
nb_filters = 64
# convolution kernel size
nb_conv = 3

batch_size = 50
if K.image_dim_ordering() == 'th':
    original_img_size = (img_chns, img_rows, img_cols)
else:
    original_img_size = (img_rows, img_cols, img_chns)
latent_dim = 2
intermediate_dim = 128
epsilon_std = 0.01
nb_epoch = 500


class BinaryEmbeddingAE(object):
    """
    Binary Embedding Conv-Deconv Autoencoder.
    """

    def __init__(self):
        x = Input(batch_shape=(batch_size,) + original_img_size)
        conv_1 = Convolution2D(img_chns, 2, 2, border_mode='same', activation='relu')(x)
        conv_2 = Convolution2D(nb_filters, 2, 2,
                               border_mode='same', activation='relu',
                               subsample=(2, 2))(conv_1)
        conv_3 = Convolution2D(nb_filters, nb_conv, nb_conv,
                               border_mode='same', activation='relu',
                               subsample=(2, 2))(conv_2)
        conv_4 = Convolution2D(nb_filters, nb_conv, nb_conv,
                               border_mode='same', activation='relu',
                               subsample=(2, 2))(conv_3)
        print(conv_4.get_shape().as_list())
        flat = Flatten()(conv_4)
        hidden = Dense(intermediate_dim, activation='relu')(flat)

        z_mean = Dense(latent_dim)(hidden)
        z_log_var = Dense(latent_dim)(hidden)

        def sampling(args):
            z_mean, z_log_var = args
            epsilon = K.random_normal(shape=(batch_size, latent_dim),
                                      mean=0., std=epsilon_std)
            return z_mean + K.exp(z_log_var) * epsilon

        # note that "output_shape" isn't necessary with the TensorFlow backend
        # so you could write `Lambda(sampling)([z_mean, z_log_var])`
        z = Lambda(sampling, output_shape=(latent_dim,))([z_mean, z_log_var])

        # we instantiate these layers separately so as to reuse them later
        decoder_hid = Dense(intermediate_dim, activation='relu')
        decoder_upsample = Dense(nb_filters * 6 * 6, activation='relu')

        if K.image_dim_ordering() == 'th':
            output_shape = (batch_size, nb_filters, 6, 6)
        else:
            output_shape = (batch_size, 6, 6, nb_filters)

        decoder_reshape = Reshape(output_shape[1:])

        if K.image_dim_ordering() == 'th':
            output_shape = (batch_size, nb_filters, 11, 11)
        else:
            output_shape = (batch_size, 11, 11, nb_filters)
        decoder_deconv_1 = Deconvolution2D(nb_filters, nb_conv, nb_conv,
                                           output_shape,
                                           border_mode='same',
                                           subsample=(2, 2),
                                           activation='relu')

        if K.image_dim_ordering() == 'th':
            output_shape = (batch_size, nb_filters, 21, 21)
        else:
            output_shape = (batch_size, 21, 21, nb_filters)

        decoder_deconv_2 = Deconvolution2D(nb_filters, nb_conv, nb_conv,
                                           output_shape,
                                           border_mode='same',
                                           subsample=(2, 2),
                                           activation='relu')
        if K.image_dim_ordering() == 'th':
            output_shape = (batch_size, nb_filters, 42, 42)
        else:
            output_shape = (batch_size, 42, 42, nb_filters)
        decoder_deconv_3_upsamp = Deconvolution2D(nb_filters, 2, 2,
                                                  output_shape,
                                                  border_mode='valid',
                                                  subsample=(2, 2),
                                                  activation='relu')
        decoder_mean_squash = Convolution2D(img_chns, 2, 2,
                                            border_mode='same',
                                            activation='sigmoid')

        hid_decoded = decoder_hid(z)
        up_decoded = decoder_upsample(hid_decoded)
        reshape_decoded = decoder_reshape(up_decoded)
        deconv_1_decoded = decoder_deconv_1(reshape_decoded)
        deconv_2_decoded = decoder_deconv_2(deconv_1_decoded)
        x_decoded_relu = decoder_deconv_3_upsamp(deconv_2_decoded)
        x_decoded_mean_squash = decoder_mean_squash(x_decoded_relu)

        def vae_loss(x, x_decoded_mean):
            # NOTE: binary_crossentropy expects a batch_size by dim
            # for x and x_decoded_mean, so we MUST flatten these!
            x = K.flatten(x)
            x_decoded_mean = K.flatten(x_decoded_mean)
            xent_loss = img_rows * img_cols * objectives.binary_crossentropy(x, x_decoded_mean)
            kl_loss = - 0.5 * K.mean(1 + z_log_var - K.square(z_mean) - K.exp(z_log_var), axis=-1)
            return xent_loss + kl_loss

        self.vae = Model(x, x_decoded_mean_squash)
        self.vae.compile(optimizer='rmsprop', loss=vae_loss)
        self.vae.summary()

        # build a digit generator that can sample from the learned distribution
        decoder_input = Input(shape=(latent_dim,))
        _hid_decoded = decoder_hid(decoder_input)
        _up_decoded = decoder_upsample(_hid_decoded)
        _reshape_decoded = decoder_reshape(_up_decoded)
        _deconv_1_decoded = decoder_deconv_1(_reshape_decoded)
        _deconv_2_decoded = decoder_deconv_2(_deconv_1_decoded)
        _x_decoded_relu = decoder_deconv_3_upsamp(_deconv_2_decoded)
        _x_decoded_mean_squash = decoder_mean_squash(_x_decoded_relu)
        self.generator = Model(decoder_input, _x_decoded_mean_squash)

        # build a model to project inputs on the latent space
        self.encoder = Model(x, z_mean)


ae = BinaryEmbeddingAE()

from sandbox.rein.dynamics_models.utils import load_dataset_atari

atari_dataset = load_dataset_atari('/Users/rein/programming/datasets/dataset_42x42.pkl')
x_train = atari_dataset['x'].transpose((0, 2, 3, 1))
print('x_train.shape:', x_train.shape)

ae.vae.fit(x_train, x_train,
           shuffle=True,
           nb_epoch=nb_epoch,
           batch_size=batch_size,
           validation_data=(x_train, x_train))

# display a 2D plot of the digit classes in the latent space
x_test_encoded = ae.encoder.predict(x_train, batch_size=batch_size)

# display a 2D manifold of the digits
n = 9  # figure with 15x15 digits
digit_size = 42
figure = np.zeros((digit_size * n, digit_size * n))
# we will sample n points within [-15, 15] standard deviations
grid_x = np.linspace(-15, 15, n)
grid_y = np.linspace(-15, 15, n)

for i, yi in enumerate(grid_x):
    for j, xi in enumerate(grid_y):
        z_sample = np.array([[xi, yi]])
        z_sample = np.tile(z_sample, batch_size).reshape(batch_size, 2)
        x_decoded = ae.generator.predict(z_sample, batch_size=batch_size)
        digit = x_decoded[0].reshape(digit_size, digit_size)
        figure[i * digit_size: (i + 1) * digit_size,
        j * digit_size: (j + 1) * digit_size] = digit

plt.figure(figsize=(10, 10))
plt.imshow(figure)
plt.show()
