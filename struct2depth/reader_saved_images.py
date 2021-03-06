# Copyright 2018 The TensorFlow Authors All Rights Reserved.
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

"""Creates a TensorFlow Dataset pipeline based off images saved to disk. Receives image sequences,
   corresponding segmentation masks, and camera intrinsics and feeds them into a Dataset.
   The Datasets are then pre-processed with random flipping, color augmentation, and shuffling to improve dataset
   quality."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import sys
from absl import logging
import tensorflow as tf
import numpy as np
import cv2
import matplotlib.pyplot as plt
import glob

import util
# from struct2depth.process_image import ImageProcessor
from process_image import ImageProcessor
import time

gfile = tf.gfile
AUTOTUNE = tf.data.experimental.AUTOTUNE

# See nets.encoder_resnet as reference for below input-normalizing constants.
IMAGENET_MEAN = (0.485, 0.456, 0.406)
IMAGENET_SD = (0.229, 0.224, 0.225)

FLIP_RANDOM = 'random'  # Always perform random flipping.
FLIP_ALWAYS = 'always'  # Always flip image input, used for test augmentation.
FLIP_NONE = 'none'  # Always disables flipping.

'''Isaac SDK code'''
# Root directory of the Isaac
ROOT_DIR = os.path.abspath("/mnt/isaac_2019_2")
sys.path.append(ROOT_DIR)

# Op names.
COLOR_IMAGE = 'image'
SEG_MASK = 'segmentation_mask'
CAMERA_MAT = 'camera_matrix'
INTRINSICS = 'camera_matrix'
INTRINSICS_INV = 'camera_matrix_inverse'
IMAGE_NORM = 'imagenet_norm'


def load_and_preprocess_image(path):
    image = tf.io.read_file(path)
    image = tf.image.decode_png(image, channels=3)
    image = tf.image.resize(image, [128, 1248])
    return image


def display_images(image_ds):
    plt.figure(figsize=(8, 8))
    for n, image in enumerate(image_ds.take(4)):
        plt.subplot(2, 2, n + 1)
        plt.imshow(image[:, :, 3])
        plt.grid(False)
        plt.xticks([])
        plt.yticks([])
        plt.show()


class DataReader(object):
    """Reads stored sequences which are produced by dataset/gen_data.py."""

    def __init__(self,
                 data_dir,
                 batch_size,
                 img_height,
                 img_width,
                 seq_length,
                 num_scales,
                 file_extension,
                 random_scale_crop,
                 flipping_mode,
                 random_color,
                 imagenet_norm,
                 shuffle,
                 isaac_app=None,
                 optimize=False,
                 repetitions=0):
        self.data_dir = data_dir
        self.batch_size = batch_size
        self.img_height = img_height
        self.img_width = img_width
        self.seq_length = seq_length
        self.num_scales = num_scales
        self.file_extension = file_extension
        self.random_scale_crop = random_scale_crop
        self.flipping_mode = flipping_mode
        self.random_color = random_color
        self.imagenet_norm = imagenet_norm
        self.shuffle = shuffle
        self.isaac_app = isaac_app
        self.steps_per_epoch = 0 # Updated once image paths are loaded
        self.optimize = optimize
        self.repetition = repetitions

    def sort_paths(self, paths, root_dir, extension):
        training_steps = []
        for i in range(len(paths)):
            training_steps.append(int(paths[i].split('/')[5].split('.')[0].split('-')[0]))

        # Sort numbers
        training_steps = sorted(training_steps)

        sorted_paths = []
        for i in range(len(training_steps)):
            sorted_paths.append(os.path.join(root_dir, str(training_steps[i]) + extension))

        return sorted_paths

    def read_data(self):
        """Provides images and camera intrinsics."""
        with tf.name_scope('data_loading'):

            if self.data_dir.endswith('/'):
                self.data_dir = self.image_dir[:-1]

            all_image_paths = [f for f in sorted(glob.glob(self.data_dir + '/*.png')) if not 'fseg' in f]
            all_image_paths_seg = sorted(glob.glob(self.data_dir + '/*-fseg.png'))
            all_image_paths_intrinsics = sorted(glob.glob(self.data_dir + '/*_cam.csv'))

            # Add multiple iterations of each path if performing online refinement
            all_image_paths_optimized = []
            all_image_paths_seg_optimized = []
            all_image_paths_intrinsics_optimized = []
            if self.optimize:
                for i in range(len(all_image_paths)):
                    for j in range(self.repetition):
                        all_image_paths_optimized.append(all_image_paths[i])
                        all_image_paths_seg_optimized.append(all_image_paths_seg[i])
                        all_image_paths_intrinsics_optimized.append(all_image_paths_intrinsics[i])

                all_image_paths = all_image_paths_optimized
                all_image_paths_seg = all_image_paths_seg_optimized
                all_image_paths_intrinsics = all_image_paths_intrinsics_optimized

            # Update steps per epoch
            self.steps_per_epoch = int(len(all_image_paths)) / self.batch_size

            # Raw image triplets
            path_ds = tf.data.Dataset.from_tensor_slices(all_image_paths)
            image_ds = path_ds.map(load_and_preprocess_image, num_parallel_calls=AUTOTUNE)

            # Seg masks
            path_ds_seg = tf.data.Dataset.from_tensor_slices(all_image_paths_seg)
            seg_ds = path_ds_seg.map(load_and_preprocess_image, num_parallel_calls=AUTOTUNE)
            seg_ds = seg_ds.map(lambda x: tf.cast(x, dtype=tf.uint8), num_parallel_calls=AUTOTUNE)  # Must be uint8

            # Camera intrinsics
            record_defaults = [tf.float32] * 9
            intrinsics_ds = tf.data.experimental.CsvDataset(all_image_paths_intrinsics,
                                                            record_defaults)  # Dataset of .csv lines
            intrinsics_ds = intrinsics_ds.map(lambda *x: tf.convert_to_tensor(x))  # Convert to tensors
            intrinsics_ds = intrinsics_ds.map(lambda x: tf.reshape(x, [3, 3]))

            logging.info("Datasets loaded")
            logging.info("Image dataset dimensions: {}".format(image_ds))
            logging.info("Seg mask dataset dimensions: {}".format(seg_ds))
            logging.info("Intrinsics dataset dimensions: {}".format(intrinsics_ds))

        with tf.name_scope('preprocessing'):

            # Scale image values from 0-255 to 0-1
            image_ds = image_ds.map(lambda x: x / 255.0, num_parallel_calls=AUTOTUNE)

            # Randomly augment colorspace
            if self.random_color:
                with tf.name_scope('image_augmentation'):
                    image_ds = image_ds.map(self.augment_image_colorspace, num_parallel_calls=AUTOTUNE)
                logging.info("Image successfully augmented")

            # Unpack triplets; each tensor is unpacked into a stack of three images
            image_stack_ds = image_ds.map(self.unpack_images, num_parallel_calls=AUTOTUNE)
            seg_stack_ds = seg_ds.map(self.unpack_images, num_parallel_calls=AUTOTUNE)
            logging.info("Images unpacked")

            # Randomly flip images
            if self.flipping_mode != FLIP_NONE:
                random_flipping = (self.flipping_mode == FLIP_RANDOM)
                with tf.name_scope('image_augmentation_flip'):
                    # Create image flipper
                    flipper = Flipper(image_width=self.img_width, randomized=random_flipping)

                    # Flip images, seg masks, and intrinsics randomly or completely, depending on random_flipping
                    image_stack_ds = image_stack_ds.map(flipper.flip_images, num_parallel_calls=AUTOTUNE)
                    seg_stack_ds = seg_stack_ds.map(flipper.flip_images, num_parallel_calls=AUTOTUNE)
                    intrinsics_ds = intrinsics_ds.map(flipper.flip_intrinsics, num_parallel_calls=AUTOTUNE)

                    logging.info("Images flipped and intrinsics adjusted")

            # Randomly scale and crop images
            if self.random_scale_crop:
                with tf.name_scope('image_augmentation_scale_crop'):
                    # Create image cropper
                    cropper = Cropper(image_width=self.img_width, image_height=self.img_height)

                    # Crop images, seg masks, and intrinsics
                    image_stack_ds = image_stack_ds.map(cropper.scale_and_crop_image, num_parallel_calls=AUTOTUNE)
                    seg_stack_ds = seg_stack_ds.map(cropper.scale_and_crop_image, num_parallel_calls=AUTOTUNE)
                    intrinsics_ds = intrinsics_ds.map(cropper.scale_and_crop_intrinsics, num_parallel_calls=AUTOTUNE)

                    logging.info("Images scaled and cropped")

            # Adjust camera intrinsics to the correct scale and compute the inverse
            with tf.name_scope('multi_scale_intrinsics'):
                intrinsics_ds = intrinsics_ds.map(self.get_multi_scale_intrinsics, num_parallel_calls=AUTOTUNE)
                intrinsics_inv = intrinsics_ds.map(lambda x: tf.matrix_inverse(x), num_parallel_calls=AUTOTUNE)

                logging.info("Multi scale intrinsics received")

            # Normalize images by the Imagenet standard
            if self.imagenet_norm:
                image_stack_norm = image_stack_ds.map(self.normalize_by_imagenet, num_parallel_calls=AUTOTUNE)
                logging.info("Imagenet norm used")
            else:
                image_stack_norm = image_stack_ds
                logging.info("Imagenet norm not used")

        # Shuffle and batch datasets
        with tf.name_scope('batching'):
            if self.shuffle:
                image_stack_ds = image_stack_ds.shuffle(buffer_size=1500, seed=2).batch(self.batch_size,
                                                                                        drop_remainder=True).repeat()
                image_stack_norm = image_stack_norm.shuffle(buffer_size=1500, seed=2).batch(self.batch_size,
                                                                                            drop_remainder=True).repeat()
                seg_stack_ds = seg_stack_ds.shuffle(buffer_size=1500, seed=2).batch(self.batch_size,
                                                                                    drop_remainder=True).repeat()
                intrinsics_ds = intrinsics_ds.shuffle(buffer_size=1500, seed=2).batch(self.batch_size,
                                                                                      drop_remainder=True).repeat()
                intrinsics_inv = intrinsics_inv.shuffle(buffer_size=1500, seed=2).batch(self.batch_size,
                                                                                        drop_remainder=True).repeat()

            else:
                image_stack_ds = image_stack_ds.batch(self.batch_size)
                image_stack_norm = image_stack_norm.batch(self.batch_size)
                seg_stack_ds = seg_stack_ds.batch(self.batch_size)
                intrinsics_ds = intrinsics_ds.batch(self.batch_size)
                intrinsics_inv = intrinsics_inv.batch(self.batch_size)

        # Create iterators over datasets
        image_it = image_stack_ds.make_one_shot_iterator().get_next()
        image_norm_it = image_stack_norm.make_one_shot_iterator().get_next()
        seg_it = seg_stack_ds.make_one_shot_iterator().get_next()
        intrinsics_it = intrinsics_ds.make_one_shot_iterator().get_next()
        intrinsics_inv_it = intrinsics_inv.make_one_shot_iterator().get_next()

        logging.info("Dataset successfuly processed")
        logging.info("Final image dimensions: {}".format(image_it))
        logging.info("Final image norm dimensions: {}".format(image_norm_it))
        logging.info("Final seg mask dimensions: {}".format(seg_it))
        logging.info("Final intrinsics dimensions: {}".format(intrinsics_it))
        logging.info("Final intrinsics inverse dimensions: {}".format(intrinsics_inv_it))

        return (image_it,
                image_norm_it,
                seg_it,
                intrinsics_it,
                intrinsics_inv_it)

    # Unpack image triplet from [h, w * seq_length, 3] -> [h, w, 3 * seq_length] image stack.
    def unpack_images(self, image_seq):
        with tf.name_scope('unpack_images'):
            image_list = [
                image_seq[:, i * self.img_width:(i + 1) * self.img_width, :]
                for i in range(self.seq_length)
            ]
            image_stack = tf.concat(image_list, axis=2)
            image_stack.set_shape([self.img_height, self.img_width, self.seq_length * 3])
        return image_stack

    # Randomly augment the brightness contrast, saturation, and hue of the image.
    # Provides more variety in training set to avoid overfitting.
    def augment_image_colorspace(self, image_stack):
        image_stack_aug = image_stack
        # Randomly shift brightness.
        apply_brightness = tf.less(tf.random_uniform(
            shape=[], minval=0.0, maxval=1.0, dtype=tf.float32), 0.5)
        image_stack_aug = tf.cond(
            apply_brightness,
            lambda: tf.image.random_brightness(image_stack_aug, max_delta=0.1),
            lambda: image_stack_aug)

        # Randomly shift contrast.
        apply_contrast = tf.less(tf.random_uniform(
            shape=[], minval=0.0, maxval=1.0, dtype=tf.float32), 0.5)
        image_stack_aug = tf.cond(
            apply_contrast,
            lambda: tf.image.random_contrast(image_stack_aug, 0.85, 1.15),
            lambda: image_stack_aug)

        # Randomly change saturation.
        apply_saturation = tf.less(tf.random_uniform(
            shape=[], minval=0.0, maxval=1.0, dtype=tf.float32), 0.5)
        image_stack_aug = tf.cond(
            apply_saturation,
            lambda: tf.image.random_saturation(image_stack_aug, 0.85, 1.15),
            lambda: image_stack_aug)

        # Randomly change hue.
        apply_hue = tf.less(tf.random_uniform(
            shape=[], minval=0.0, maxval=1.0, dtype=tf.float32), 0.5)
        image_stack_aug = tf.cond(
            apply_hue,
            lambda: tf.image.random_hue(image_stack_aug, max_delta=0.1),
            lambda: image_stack_aug)

        # Clip image to be between 0 and 1
        image_stack_aug = tf.clip_by_value(image_stack_aug, 0, 1)
        return image_stack_aug

    # Creates multi scale intrinsics based off number of scales provided.
    def get_multi_scale_intrinsics(self, intrinsics):
        intrinsics_multi_scale = []
        # Scale the intrinsics accordingly for each scale
        for s in range(self.num_scales):
            fx = intrinsics[0, 0] / (2 ** s)
            fy = intrinsics[1, 1] / (2 ** s)
            cx = intrinsics[0, 2] / (2 ** s)
            cy = intrinsics[1, 2] / (2 ** s)
            intrinsics_multi_scale.append(make_intrinsics_matrix(fx, fy, cx, cy))
        intrinsics_multi_scale = tf.stack(intrinsics_multi_scale)
        return intrinsics_multi_scale

    # Normalize the image by the Imagenet mean and standard deviation.
    # This aligns the training dataset with the pre-trained Imagenet model, and allows
    # for standardized evaluation of test set.
    def normalize_by_imagenet(self, image_stack):
        # Copy constant values multiple times to fill up a tensor of length SEQ_LENGTH * len(IMAGENET_MEAN)
        im_mean = tf.tile(
            tf.constant(IMAGENET_MEAN), multiples=[self.seq_length])
        im_sd = tf.tile(
            tf.constant(IMAGENET_SD), multiples=[self.seq_length])
        return (image_stack - im_mean) / im_sd


# Class for flipping images, seg masks, and intrinsics
# Provides greater variety in training dataset to avoid overfitting
class Flipper:
    def __init__(self,
                 image_width=416,
                 randomized=True):
        self.randomized = randomized
        self.img_width = image_width  # Assumes all input images are the same size

    # Randomly flips the image horizontally.
    def flip_images(self, image_stack):

        if self.randomized:
            # Generate random number. Seed provided to ensure that image,
            # seg mask, and intrinsics are paired
            prob = tf.random_uniform(shape=[], minval=0.0, maxval=1.0,
                                     dtype=tf.float32, seed=2)
            predicate = tf.less(prob, 0.5)
        else:
            predicate = tf.less(0.0, 0.5)

        return tf.cond(predicate,
                       lambda: tf.image.flip_left_right(image_stack),
                       lambda: image_stack)

    # Randomly flips intrinsics.
    def flip_intrinsics(self, intrinsics):

        def flip(intrinsics, in_w):
            fx = intrinsics[0, 0]
            fy = intrinsics[1, 1]
            cx = in_w - intrinsics[0, 2]
            cy = intrinsics[1, 2]
            return make_intrinsics_matrix(fx, fy, cx, cy)

        if self.randomized:
            # Generate random probability. Seed provided to ensure that image,
            # seg mask, and intrinsics are paired
            prob = tf.random_uniform(shape=[], minval=0.0, maxval=1.0,
                                     dtype=tf.float32, seed=2)
            predicate = tf.less(prob, 0.5)
        else:
            predicate = tf.less(0.0, 0.5)

        return tf.cond(predicate,
                       lambda: flip(intrinsics, self.img_width),
                       lambda: intrinsics)


# Class for cropping images. First scales them to provide a greater area
# to crop from.
class Cropper:
    def __init__(self,
                 image_width=416,
                 image_height=128):
        self.orig_img_width = image_width
        self.orig_img_height = image_height
        self.scaled_img_width = image_width  # New image width before cropping to original dimensions
        self.scaled_img_height = image_height  # New image height before cropping to original dimensions

    # Scale and crop image.
    def scale_and_crop_image(self, im):
        im = self.scale_images_randomly(im)
        im = self.crop_images_randomly(im)
        return im

    # Scale image randomly. Seed used to match corresponding image, seg mask, and intrinsics.
    # Scales to a random number with greater dimensions than the original.
    def scale_images_randomly(self, im):
        scaling = tf.random_uniform([2], 1, 1.15, seed=2)
        x_scaling = scaling[0]
        y_scaling = scaling[1]
        self.scaled_img_height = tf.cast(self.orig_img_height * y_scaling, dtype=tf.int32)
        self.scaled_img_width = tf.cast(self.orig_img_width * x_scaling, dtype=tf.int32)

        # Add batch to resize the image area, then revert back
        im = tf.expand_dims(im, 0)
        im = tf.image.resize_area(im, [self.scaled_img_height, self.scaled_img_width])
        return im[0]

    # Crop image randomly. Outputs image with its original height and width.
    def crop_images_randomly(self, im):
        offset_y = tf.random_uniform([1], 0, self.scaled_img_height - self.orig_img_height + 1, dtype=tf.int32, seed=2)[
            0]
        offset_x = tf.random_uniform([1], 0, self.scaled_img_width - self.orig_img_width + 1, dtype=tf.int32, seed=2)[0]
        im = tf.image.crop_to_bounding_box(im, offset_y, offset_x, self.orig_img_height, self.orig_img_width)
        return im

    # Scales and crops intrinsics, keeping them matched with their corresponding image stacks.
    # Make sure to scale and crop images first, as the randomly scaled image widths and heights are needed to
    # scale and crop intrinsics.
    def scale_and_crop_intrinsics(self, intrinsics):
        intrinsics = self.scale_intrinsics_randomly(intrinsics)
        intrinsics = self.crop_intrinsics_randomly(intrinsics)
        return intrinsics

    # Scale intrinsics randomly. Seed used to match corresponding image, seg mask, and intrinsics.
    def scale_intrinsics_randomly(self, intrinsics):
        scaling = tf.random_uniform([2], 1, 1.15, seed=2)
        x_scaling = scaling[0]
        y_scaling = scaling[1]

        fx = intrinsics[0, 0] * x_scaling
        fy = intrinsics[1, 1] * y_scaling
        cx = intrinsics[0, 2] * x_scaling
        cy = intrinsics[1, 2] * y_scaling
        return make_intrinsics_matrix(fx, fy, cx, cy)

    # Crop intrinsics randomly. It is assumed that the images has already been cropped, so that
    # the scaled image height and width are already known and saved in class state.
    def crop_intrinsics_randomly(self, intrinsics):
        """Crops image and adjust intrinsics accordingly."""
        offset_y = tf.random_uniform([1], 0, self.scaled_img_height - self.orig_img_height + 1, dtype=tf.int32, seed=2)[
            0]
        offset_x = tf.random_uniform([1], 0, self.scaled_img_width - self.orig_img_width + 1, dtype=tf.int32, seed=2)[0]
        fx = intrinsics[0, 0]
        fy = intrinsics[1, 1]
        cx = intrinsics[0, 2] - tf.cast(offset_x, dtype=tf.float32)
        cy = intrinsics[1, 2] - tf.cast(offset_y, dtype=tf.float32)
        return make_intrinsics_matrix(fx, fy, cx, cy)


# Creates a 3x3 intrinsics matrix from camera essentials.
# Returned as a tf.float32 tensor
def make_intrinsics_matrix(fx, fy, cx, cy):
    r1 = tf.stack([fx, 0, cx])
    r2 = tf.stack([0, fy, cy])
    r3 = tf.constant([0., 0., 1.])
    intrinsics = tf.stack([r1, r2, r3])
    return intrinsics
