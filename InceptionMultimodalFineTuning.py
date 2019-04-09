# PURPOSE:
# InceptionV3 fine tuning for hepatocarcinoma diagnosis through CTs images
# with image augmentation and multimodal inputs

import os
from keras.applications.inception_v3 import InceptionV3
from keras.models import Model
from keras.layers import Dense, GlobalAveragePooling2D
from keras.layers import Conv2D, MaxPooling2D, Input, concatenate
from keras.layers import Activation, Dropout, Flatten, Dense, BatchNormalization
from keras.preprocessing.image import ImageDataGenerator
from keras.callbacks import ModelCheckpoint, EarlyStopping
from keras.optimizers import SGD
import numpy as np
from Summary import create_results_dir, get_base_name, plot_train_stats, write_summary_txt, copy_to_s3
from ExecutionAttributes import ExecutionAttribute
from TimeCallback import TimeCallback
from TrainingResume import save_execution_attributes
from keras.utils import plot_model
from Datasets import load_data, create_image_generator, multimodal_generator_two_inputs

# fix seed for reproducible results (only works on CPU, not GPU)
# seed = 9
# np.random.seed(seed=seed)
# tf.set_random_seed(seed=seed)

# Summary Information
SUMMARY_PATH = "/mnt/data/results"
# SUMMARY_PATH="c:/temp/results"
# SUMMARY_PATH="/tmp/results"
NETWORK_FORMAT = "Multimodal"
IMAGE_FORMAT = "2D"
SUMMARY_BASEPATH = create_results_dir(SUMMARY_PATH, NETWORK_FORMAT, IMAGE_FORMAT)
INTERMEDIATE_FUSION = False
LATE_FUSION = True

# Execution Attributes
attr = ExecutionAttribute()
attr.architecture = 'InceptionV3'
numpy_path = '/mnt/data/image/2d/numpy/sem_pre_proc/'

results_path = create_results_dir(SUMMARY_BASEPATH, 'fine-tuning', attr.architecture)
attr.summ_basename = get_base_name(results_path)
# attr.path = '/mnt/data/image/2d/com_pre_proc'
attr.set_dir_names()
attr.batch_size = 32  # try 4, 8, 16, 32, 64, 128, 256 dependent on CPU/GPU memory capacity (powers of 2 values).
attr.epochs = 1

# create the base pre-trained model
base_model = InceptionV3(weights='imagenet', include_top=False)

# dimensions of our images.
# Inception input size
attr.img_width, attr.img_height = 299, 299

input_attributes_s = (20,)

images_train, fnames_train, attributes_train, labels_train, \
    images_valid, fnames_valid, attributes_valid, labels_valid, \
    images_test, fnames_test, attributes_test, labels_test = load_data(numpy_path)

# Top Model Block
glob1 = GlobalAveragePooling2D()(base_model.output)

if INTERMEDIATE_FUSION:
    attr.fusion = "Intermediate Fusion"

    attributes_input = Input(shape=input_attributes_s)
    concat = concatenate([glob1, attributes_input])

    hidden1 = Dense(512, activation='relu')(concat)
    output = Dense(nb_classes, activation='softmax')(hidden1)

if LATE_FUSION:
    attr.fusion = "Late Fusion"
    hidden1 = Dense(512, activation='relu')(glob1)
    output_img = Dense(nb_classes, activation='softmax')(hidden1)

    attributes_input = Input(shape=input_attributes_s)
    hidden3 = Dense(32, activation='relu')(attributes_input)
    drop6 = Dropout(0.2)(hidden3)
    hidden4 = Dense(16, activation='relu')(drop6)
    drop7 = Dropout(0.2)(hidden4)
    output_attributes = Dense(1, activation='sigmoid')(drop7)

    concat = concatenate([output_img, output_attributes])
    hidden5 = Dense(4, activation='relu')(concat)
    output = Dense(1, activation='sigmoid')(hidden5)

attr.model = Model(inputs=[base_model.input, attributes_input], outputs=output)

plot_model(attr.model, to_file=attr.summ_basename + '-architecture.png')

# first: train only the top layers (which were randomly initialized)
# i.e. freeze all convolutional InceptionV3 layers
for layer in base_model.layers:
    layer.trainable = False

# compile the model (should be done *after* setting layers to non-trainable)
attr.model.compile(optimizer='rmsprop', loss='categorical_crossentropy', metrics=['accuracy'], )

# calculate steps based on number of images and batch size
attr.train_samples = len(images_train)
attr.valid_samples = len(images_valid)
attr.test_samples = len(images_test)

train_datagen = create_image_generator(True, True)

test_datagen = create_image_generator(True, False)

attr.train_generator = multimodal_generator_two_inputs(images_train, attributes_train, labels_train, train_datagen, attr.batch_size)
attr.validation_generator = multimodal_generator_two_inputs(images_valid, attributes_valid, labels_valid, test_datagen, attr.batch_size)
attr.test_generator = multimodal_generator_two_inputs(images_test, attributes_test, labels_test, test_datagen, 1)

callbacks_top = [
    ModelCheckpoint(attr.summ_basename + "-mid-ckweights.h5", monitor='val_acc', verbose=1, save_best_only=True),
    EarlyStopping(monitor='val_loss', patience=10, verbose=0)
]

# calculate steps based on number of images and batch size
attr.calculate_steps()

attr.increment_seq()

# Persist execution attributes for session resume
save_execution_attributes(attr, attr.summ_basename + '-execution-attributes.properties')

attr.model.fit_generator(
    attr.train_generator,
    steps_per_epoch=attr.steps_train,
    epochs=attr.epochs,
    validation_data=attr.validation_generator,
    validation_steps=attr.steps_valid,
    callbacks=callbacks_top)

# at this point, the top layers are well trained and we can start fine-tuning
# convolutional layers from inception V3. We will freeze the bottom N layers
# and train the remaining top layers.

# let's visualize layer names and layer indices to see how many layers
# we should freeze:
for i, layer in enumerate(base_model.layers):
    print(i, layer.name)

attr.model.load_weights(attr.summ_basename + "-mid-ckweights.h5")

time_callback = TimeCallback()

#Save the model after every epoch.
callbacks_list = [time_callback,
    ModelCheckpoint(attr.summ_basename + "-ckweights.h5", monitor='val_acc', verbose=1, save_best_only=True),
    EarlyStopping(monitor='val_loss', patience=10, verbose=0)
]

# train the top 2 inception blocks, i.e. we will freeze
# the first 172 layers and unfreeze the rest:
for layer in attr.model.layers[:172]:
    layer.trainable = False
for layer in attr.model.layers[172:]:
    layer.trainable = True

# we need to recompile the model for these modifications to take effect
# we use SGD with a low learning rate
attr.model.compile(optimizer=SGD(lr=0.0001, momentum=0.9), loss='categorical_crossentropy', metrics=['accuracy'])

plot_model(attr.model, to_file=attr.summ_basename + '-architecture.png')

history = attr.model.fit_generator(
    attr.train_generator,
    steps_per_epoch=attr.steps_train,
    epochs=attr.epochs,
    validation_data=attr.validation_generator,
    validation_steps=attr.steps_valid,
    callbacks=callbacks_list)

# Save the model
attr.model.save(attr.summ_basename + '-weights.h5')

# Plot train stats
plot_train_stats(history, attr.summ_basename + '-training_loss.png', attr.summ_basename + '-training_accuracy.png')

# Get the filenames from the generator
fnames = attr.fnames_test

# Get the ground truth from generator
ground_truth = attr.labels_test

# Get the predictions from the model using the generator
predictions = attr.model.predict_generator(attr.test_generator, steps=attr.steps_test, verbose=1)
predicted_classes = np.argmax(predictions, axis=1)

errors = np.where(predicted_classes != ground_truth)[0]
res = "No of errors = {}/{}".format(len(errors), len(attr.fnames_test))
with open(attr.summ_basename + "-predicts.txt", "a") as f:
    f.write(res)
    print(res)
    f.close()

write_summary_txt(attr, NETWORK_FORMAT, IMAGE_FORMAT, ['negative', 'positive'], time_callback, callbacks_list[2].stopped_epoch)

# copy_to_s3(attr)