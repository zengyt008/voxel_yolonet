#!/usr/bin/env python
# -*- coding:UTF-8 -*-

# File Name : kitti_loader.py
# Purpose :
# Creation Date : 09-12-2017
# Last Modified : 2017年12月12日 星期二 21时26分43秒
# Created By : Jeasine Ma [jeasinema[at]gmail[dot]com]

import cv2
import numpy as np
import os
import sys
import glob
import threading
import time
import math
import random
from sklearn.utils import shuffle
from multiprocessing import Lock, Process, Queue as Queue, Value, Array, cpu_count

from config import cfg
from misc_util import warn
from utils.preprocess import voxelize
from data_aug import data_augmentation
from utils.preprocess import clip_by_projection
from utils.utils import read_calib_mat 
# from PIL import Image

# for non-raw dataset
class KittiLoader(object):

    # return: 
    # tag (N)
    # label (N) (N') (just raw string in the label files) (when it is not test set)
    # rgb (N, H, W, C)
    # raw_lidar (N) (N', 4)
    # vox_feature 
    # vox_number 
    # vox_coordinate 

    def __init__(self, object_dir='.', queue_size=20, require_shuffle=False, is_testset=True, batch_size=1, use_multi_process_num=0, split_file='', valid_file='', multi_gpu_sum=1):
        assert(use_multi_process_num >= 0)
        self.object_dir = object_dir
        self.is_testset = is_testset
        self.use_multi_process_num = use_multi_process_num if not self.is_testset else 1
        self.require_shuffle = require_shuffle if not self.is_testset else False
        self.batch_size=batch_size if not self.is_testset else 1
        self.split_file = split_file 
        self.valid_file = valid_file
        self.multi_gpu_sum = multi_gpu_sum
        self.progress = 0

        # warn("dir: {}".format(self.object_dir))

        if self.split_file != '':
            # use split file  
            _tag = []
            self.f_rgb, self.f_lidar, self.f_label, self.f_calib = [], [], [], []
            self.f_voxel = []
            for line in open(self.split_file, 'r').readlines():
                line = line[:-1] # remove '\n'
                _tag.append(line)
                self.f_rgb.append(os.path.join(self.object_dir, 'image_2', line+'.png'))
                self.f_lidar.append(os.path.join(self.object_dir, 'velodyne', line+'.bin'))
                self.f_label.append(os.path.join(self.object_dir, 'label_2', line+'.txt'))
                self.f_calib.append(os.path.join(self.object_dir, 'calib', line+'.txt'))


            self.f_rgb_valid = []
            self.f_lidar_valid = []
            self.f_label_valid = []
            self.f_calib_valid = []

            for line in open(self.valid_file, 'r').readlines():
                line = line[:-1] # remove '\n'
                self.f_rgb_valid.append(os.path.join(self.object_dir, 'image_2', line+'.png'))
                self.f_lidar_valid.append(os.path.join(self.object_dir, 'velodyne', line+'.bin'))
                self.f_label_valid.append(os.path.join(self.object_dir, 'label_2', line+'.txt'))
                self.f_calib_valid.append(os.path.join(self.object_dir, 'calib', line+'.txt'))
                self.data_tag_valid =  [name.split('/')[-1].split('.')[-2] for name in self.f_label_valid]


        else:
            self.f_rgb = glob.glob(os.path.join(self.object_dir, 'image_2', '*.png'))
            self.f_rgb.sort()
            self.f_lidar = glob.glob(os.path.join(self.object_dir, 'velodyne', '*.bin'))
            self.f_lidar.sort()
            self.f_label = glob.glob(os.path.join(self.object_dir, 'label_2', '*.txt'))
            self.f_label.sort()
            self.f_calib = glob.glob(os.path.join(self.object_dir, 'calib', '*.txt'))
            self.f_calib.sort()

        self.data_tag =  [name.split('/')[-1].split('.')[-2] for name in self.f_label]
        # assert(len(self.f_rgb) == len(self.f_lidar) == len(self.f_label) == len(self.data_tag))
        warn("{} {} {} {}".format(len(self.f_label), len(self.data_tag), len(self.f_lidar), len(self.f_calib)))
        assert(len(self.f_label) == len(self.data_tag) == len(self.f_rgb) == len(self.f_lidar))
        self.dataset_size = len(self.f_label)
        self.validset_size = len(self.f_label_valid)
        self.already_extract_data = 0
        self.cur_frame_info = ''

        # warn("Dataset total length: {}".format(len(self.f_label)))
        if self.require_shuffle:
            self.shuffle_dataset()

        self.queue_size = queue_size
        self.require_shuffle = require_shuffle
        self.dataset_queue = Queue()  # must use the queue provided by multiprocessing module(only this can be shared)

        self.load_index = 0
        if self.use_multi_process_num == 0:
            self.loader_worker = [threading.Thread(target=self.loader_worker_main, args=(self.batch_size,))]
        else:
            self.loader_worker = [Process(target=self.loader_worker_main, args=(self.batch_size,)) for i in range(self.use_multi_process_num)]
        self.work_exit = Value('i', 0)
        [i.start() for i in self.loader_worker]

        # This operation is not thread-safe
        self.rgb_shape = (cfg.IMAGE_HEIGHT, cfg.IMAGE_WIDTH, 3)
    
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.work_exit.value = True

    def __len__(self):
        return self.dataset_size



    def fill_queue(self, batch_size=0):
        load_index = self.load_index
        self.load_index += batch_size 
        if self.load_index >= self.dataset_size:
            if not self.is_testset:  # test set just end
                if self.require_shuffle:
                    self.shuffle_dataset()
                load_index = 0
                self.load_index = load_index + batch_size 
            else:
                self.work_exit.value = True

        if self.progress < 0.2:
            max_voxel_points = cfg.VOXEL_POINT_COUNT
        elif self.progress < 0.4:
            max_voxel_points = cfg.VOXEL_POINT_COUNT - 2 + np.random.randint(3)
        elif self.progress < 0.6:
            max_voxel_points = cfg.VOXEL_POINT_COUNT - 4 + np.random.randint(5)
        elif self.progress < 0.8:
            max_voxel_points = cfg.VOXEL_POINT_COUNT - 6 + np.random.randint(7)
        else:
            max_voxel_points = cfg.VOXEL_POINT_COUNT - 8 + np.random.randint(9)

        labels, tag, voxel, doubled_voxel, rgb, raw_lidar, calib = [], [], [], [], [], [], []
        voxel_size = np.array([cfg.VOXEL_Z_SIZE, cfg.VOXEL_Y_SIZE, cfg.VOXEL_X_SIZE], dtype=np.float32)
        double_voxel_size = 2 * voxel_size

        # warn("fill : {}".format(batch_size))

        for _ in range(batch_size):
            try:
                t0 = time.time()    
                ##### AFTER DATA AUGMENTATION STABILIZED ####
                rgb_img = cv2.imread(self.f_rgb[load_index])
                img_height, img_width, _ = rgb_img.shape
                # warn("height: {} width: {}".format(height, width))
                rgb.append(cv2.resize(rgb_img, (cfg.IMAGE_WIDTH, cfg.IMAGE_HEIGHT)))
                calib_mat = read_calib_mat(self.f_calib[load_index])
                calib.append(calib_mat.copy())
                lidar, label = data_augmentation(f_lidar = self.f_lidar[load_index], f_label = self.f_label[load_index], calib_mat=calib_mat, img_width = img_width, img_height = img_height)
                raw_lidar.append(lidar)
                labels.append(label)
                tag.append(self.data_tag[load_index])
                voxel.append(voxelize(file = self.f_lidar[load_index], lidar = lidar, voxel_size = voxel_size, T = max_voxel_points))
                doubled_voxel.append(voxelize(file = self.f_lidar[load_index], lidar = lidar, voxel_size = double_voxel_size, T = max_voxel_points))
                t1 = time.time()

                load_index += 1

                # only for voxel -> [gpu, k_single_batch, ...]
                vox_feature, vox_number, vox_coordinate = [], [], []


                single_batch_size = int(self.batch_size/self.multi_gpu_sum)
                for idx in range(self.multi_gpu_sum):
                    # warn("normal voxel")
                    _, per_vox_feature, per_vox_number, per_vox_coordinate = build_input(voxel[idx*single_batch_size:(idx+1)*single_batch_size])
                    vox_feature.append(per_vox_feature)
                    vox_number.append(per_vox_number)
                    vox_coordinate.append(per_vox_coordinate)

                doubled_vox_feature, doubled_vox_number, doubled_vox_coordinate = [], [], []            
                for idx in range(self.multi_gpu_sum):
                    # warn("doubled voxel")
                    _, per_vox_feature, per_vox_number, per_vox_coordinate = build_input(doubled_voxel[idx*single_batch_size:(idx+1)*single_batch_size])
                    doubled_vox_feature.append(per_vox_feature)
                    doubled_vox_number.append(per_vox_number)
                    doubled_vox_coordinate.append(per_vox_coordinate)

                self.dataset_queue.put_nowait((labels, (vox_feature, vox_number, vox_coordinate), (doubled_vox_feature, doubled_vox_number, doubled_vox_coordinate), rgb, raw_lidar, calib, tag))


            except:
                # warn("exception occured")
                if not self.is_testset:  # test set just end
                    self.load_index = 0
                    if self.require_shuffle:
                        self.shuffle_dataset()
                else:
                    self.work_exit.value = True
        


        # self.dataset_queue.put_nowait((labels, (vox_feature, vox_number, vox_coordinate), (doubled_vox_feature, doubled_vox_number, doubled_vox_coordinate), rgb, raw_lidar, tag))

    def load(self):
        try:
            if self.is_testset and self.already_extract_data >= self.dataset_size:
                return None
            buff = self.dataset_queue.get()
            label = buff[0]
            vox_feature = buff[1][0]
            vox_number = buff[1][1]
            vox_coordinate = buff[1][2]
            doubled_vox_feature = buff[2][0]
            doubled_vox_number = buff[2][1]
            doubled_vox_coordinate = buff[2][2]
            rgb = buff[3]
            raw_lidar = buff[4]
            calib = buff[5]
            tag = buff[6]
            self.cur_frame_info = buff[6]

            self.already_extract_data += self.batch_size

            ret = (
                np.array(tag),
                np.array(label),
                np.array(vox_feature),
                np.array(vox_number),
                np.array(vox_coordinate),
                np.array(doubled_vox_feature),
                np.array(doubled_vox_number),
                np.array(doubled_vox_coordinate),
                np.array(rgb),
                np.array(raw_lidar),
                np.array(calib)
            )
        except:
            print("Dataset empty!")
            ret = None
        return ret

    def load_specified(self, load_indices=None):
        # Load without data augmentation
        labels, tag, voxel, doubled_voxel, rgb, raw_lidar, calib = [], [], [], [], [], [], []
        voxel_size = np.array([cfg.VOXEL_Z_SIZE, cfg.VOXEL_Y_SIZE, cfg.VOXEL_X_SIZE], dtype=np.float32)
        double_voxel_size = 2 * voxel_size
        
        if load_indices is None:
            load_indices = np.random.randint(len(self.f_rgb_valid), size = self.batch_size)

        for load_index in load_indices:
            try:
                t0 = time.time()
                rgb_img = cv2.imread(self.f_rgb_valid[load_index])
                img_height, img_width, _ = rgb_img.shape

                rgb.append(cv2.resize(rgb_img, (cfg.IMAGE_WIDTH, cfg.IMAGE_HEIGHT)))
                lidar = np.fromfile(self.f_lidar_valid[load_index], dtype=np.float32).reshape((-1, 4))

                calib_file = self.f_lidar_valid[load_index].replace('velodyne', 'calib').replace('bin', 'txt')
                lidar = clip_by_projection(lidar, calib_file, img_height, img_width)

                raw_lidar.append(lidar)
                labels.append([line for line in open(self.f_label_valid[load_index], 'r').readlines()])
                calib.append(read_calib_mat(self.f_calib_valid[load_index]))
                tag.append(self.data_tag_valid[load_index])
                voxel.append(voxelize(file = self.f_lidar_valid[load_index], lidar = lidar, voxel_size = voxel_size, T = cfg.VOXEL_POINT_COUNT))
                doubled_voxel.append(voxelize(file = self.f_lidar_valid[load_index], lidar = lidar, voxel_size = double_voxel_size, T = cfg.VOXEL_POINT_COUNT))
                t1 = time.time()

            except:
                warn("Load Specified: Loading Error!!")
        
        # only for voxel -> [gpu, k_single_batch, ...]
        vox_feature, vox_number, vox_coordinate = [], [], []

        # warn("file path 1: {}".format(self.f_lidar[0]))

        single_batch_size = int(self.batch_size/self.multi_gpu_sum)
        for idx in range(self.multi_gpu_sum):
            # warn("single")
            _, per_vox_feature, per_vox_number, per_vox_coordinate = build_input(voxel[idx*single_batch_size:(idx+1)*single_batch_size])
            vox_feature.append(per_vox_feature)
            vox_number.append(per_vox_number)
            vox_coordinate.append(per_vox_coordinate)

        doubled_vox_feature, doubled_vox_number, doubled_vox_coordinate = [], [], []            
        for idx in range(self.multi_gpu_sum):
            # warn("doubled")
            _, per_vox_feature, per_vox_number, per_vox_coordinate = build_input(doubled_voxel[idx*single_batch_size:(idx+1)*single_batch_size])
            doubled_vox_feature.append(per_vox_feature)
            doubled_vox_number.append(per_vox_number)
            doubled_vox_coordinate.append(per_vox_coordinate)

        ret = (
            np.array(tag),
            np.array(labels),
            np.array(vox_feature),
            np.array(vox_number),
            np.array(vox_coordinate),
            np.array(doubled_vox_feature),
            np.array(doubled_vox_number),
            np.array(doubled_vox_coordinate),
            np.array(rgb),
            np.array(raw_lidar),
            np.array(calib)
        )

        return ret

    def load_specified_train(self, load_indices=None):
        # Load without data augmentation
        labels, tag, voxel, doubled_voxel, rgb, raw_lidar, calib = [], [], [], [], [], [], []
        voxel_size = np.array([cfg.VOXEL_Z_SIZE, cfg.VOXEL_Y_SIZE, cfg.VOXEL_X_SIZE], dtype=np.float32)
        double_voxel_size = 2 * voxel_size

        
        if load_indices is None:
            load_indices = np.random.randint(len(self.f_rgb), size = self.batch_size)

        for load_index in load_indices:
            try:
                t0 = time.time()
                rgb.append(cv2.resize(cv2.imread(self.f_rgb[load_index]), (cfg.IMAGE_WIDTH, cfg.IMAGE_HEIGHT)))
                lidar = np.fromfile(self.f_lidar[load_index], dtype=np.float32).reshape((-1, 4))

                calib_file = self.f_lidar[load_index].replace('velodyne', 'calib').replace('bin', 'txt')
                lidar = clip_by_projection(lidar, calib_file, cfg.IMAGE_HEIGHT, cfg.IMAGE_WIDTH)

                raw_lidar.append(lidar)
                calib.append(read_calib_mat(self.f_calib[load_index]))

                labels.append([line for line in open(self.f_label[load_index], 'r').readlines()])
                tag.append(self.data_tag[load_index])
                voxel.append(voxelize(file = self.f_lidar[load_index], lidar = lidar, voxel_size = voxel_size, T = cfg.VOXEL_POINT_COUNT))
                doubled_voxel.append(voxelize(file = self.f_lidar[load_index], lidar = lidar, voxel_size = double_voxel_size, T = cfg.VOXEL_POINT_COUNT))
                t1 = time.time()
                # warn("load success")

            except:
                warn("Load Specified: Loading Error!! {}".format(tag))
        
        # only for voxel -> [gpu, k_single_batch, ...]
        vox_feature, vox_number, vox_coordinate = [], [], []


        single_batch_size = int(self.batch_size/self.multi_gpu_sum)
        for idx in range(self.multi_gpu_sum):
            # warn("single")
            _, per_vox_feature, per_vox_number, per_vox_coordinate = build_input(voxel[idx*single_batch_size:(idx+1)*single_batch_size])
            vox_feature.append(per_vox_feature)
            vox_number.append(per_vox_number)
            vox_coordinate.append(per_vox_coordinate)

        doubled_vox_feature, doubled_vox_number, doubled_vox_coordinate = [], [], []            
        for idx in range(self.multi_gpu_sum):
            # warn("doubled")
            _, per_vox_feature, per_vox_number, per_vox_coordinate = build_input(doubled_voxel[idx*single_batch_size:(idx+1)*single_batch_size])
            doubled_vox_feature.append(per_vox_feature)
            doubled_vox_number.append(per_vox_number)
            doubled_vox_coordinate.append(per_vox_coordinate)

        ret = (
            np.array(tag),
            np.array(labels),
            np.array(vox_feature),
            np.array(vox_number),
            np.array(vox_coordinate),
            np.array(doubled_vox_feature),
            np.array(doubled_vox_number),
            np.array(doubled_vox_coordinate),
            np.array(rgb),
            np.array(raw_lidar),
            np.array(calib)
        )

        return ret

    def loader_worker_main(self, batch_size):
        if self.require_shuffle:
            self.shuffle_dataset()
        while not self.work_exit.value:
            if self.dataset_queue.qsize() >= self.queue_size // 2:
                # warn("sleep")
                time.sleep(1)
            else:
                # warn("fill quese")
                self.fill_queue(batch_size)  # since we use multiprocessing, 1 is ok

    def get_shape(self):
        return self.rgb_shape

    def shuffle_dataset(self):
        # to prevent diff loader load same data
        index = shuffle([i for i in range(len(self.f_label))], random_state=random.randint(0, self.use_multi_process_num**5))
        self.f_label = [self.f_label[i] for i in index]
        self.f_rgb = [self.f_rgb[i] for i in index]
        self.f_lidar = [self.f_lidar[i] for i in index]
        self.f_calib = [self.f_calib[i] for i in index]
        self.data_tag = [self.data_tag[i] for i in index]

    def get_frame_info(self):
        return self.cur_frame_info


def build_input(voxel_dict_list):
    batch_size = len(voxel_dict_list)

    # warn("kitt loader:{}".format(batch_size))

    feature_list = []
    number_list = []
    coordinate_list = []
    for i, voxel_dict in zip(range(batch_size), voxel_dict_list):
        feature_list.append(voxel_dict['feature_buffer'])
        number_list.append(voxel_dict['number_buffer'])
        coordinate = voxel_dict['coordinate_buffer']
        coordinate_list.append(
            np.pad(coordinate, ((0, 0), (1, 0)),
                   mode='constant', constant_values=i))
        # warn("build shape: {} {} {}".format(np.shape(voxel_dict['feature_buffer']), np.shape(voxel_dict['number_buffer']), np.shape(voxel_dict['coordinate_buffer'])))

    # warn("feature size:{}".format(np.shape(feature_list)))
    feature = np.concatenate(feature_list)
    number = np.concatenate(number_list)
    coordinate = np.concatenate(coordinate_list)
    return batch_size, feature, number, coordinate



if __name__ == '__main__':
    pass
