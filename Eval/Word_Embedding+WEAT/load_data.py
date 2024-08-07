#!/usr/bin/env python3
# coding: utf-8
# File: demo.py.py
# Author: lhy<lhy_in_blcu@126.com,https://huangyong.github.io>
# Date: 18-4-25

class DataLoader:
    def __init__(self):
        self.datafile = 'data/data_answer_3_21.txt'
        self.dataset = self.load_data()

    '''加载数据集'''
    def load_data(self):
        dataset = []
        for line in open(self.datafile,encoding='UTF-8'):
            line = line.strip().split(',')
            dataset.append([word for word in line[0].split(' ') if 'nbsp' not in word and len(word) < 11])
        return dataset
