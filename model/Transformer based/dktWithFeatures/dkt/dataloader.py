import os
import random
import time
from datetime import datetime
from easydict import EasyDict

import numpy as np
import pandas as pd
import torch
import tqdm
from sklearn.preprocessing import LabelEncoder


class Preprocess:
    def __init__(self, args):
        self.args = args
        self.train_data = None
        self.test_data = None

    def get_train_data(self):
        return self.train_data

    def get_test_data(self):
        return self.test_data

    def split_data(self, data, ratio=0.8, shuffle=True, seed=0):
        """
        split data into two parts with a given ratio.
        """
        if shuffle:
            random.seed(seed)  # fix to default seed 0
            random.shuffle(data)

        # data split strategy (1) default: split by user (no k-fold)
        if self.args.split_method == "user":
            size = int(len(data) * ratio)
            data_1 = data[:size]
            data_2 = data[size:]

        # data split strategy (2) split by user & k-fold
        elif self.args.split_method == "k-fold":
            data_1 = data[:]
            data_2 = None

        else:
            raise Exception(
                "알 수 없는 데이터 분할 전략입니다.\n\
                            split_method 인자로 다음을 사용하십시오 ['user', 'k-fold']"
            )

        return data_1, data_2

    def __save_labels(self, encoder, name):
        le_path = os.path.join(self.args.asset_dir, name + "_classes.npy")
        np.save(le_path, encoder.classes_)
        print(f"[3-3-2] Saving labels ({le_path})")

    def __preprocessing(self, df, is_train=True):
        """
        continus_cols -> 처리할 필요 없음
        cate_cols -> 카테고리 형태로 변환 -> LabelEncoder
        """
        # cate_cols = ["assessmentItemID", "testId", "KnowledgeTag"]

        if not os.path.exists(self.args.asset_dir):
            os.makedirs(self.args.asset_dir)

        print("\n[3-3-1] Categorical Feature Embedding (Label Encoder)")
        for col in self.args.cate_feats:

            le = LabelEncoder()
            if is_train:
                # For UNKNOWN class
                a = df[col].unique().tolist() + ["unknown"]
                le.fit(a)
                self.__save_labels(le, col)
            else:
                label_path = os.path.join(self.args.asset_dir, col + "_classes.npy")
                le.classes_ = np.load(label_path)

                df[col] = df[col].apply(
                    lambda x: x if str(x) in le.classes_ else "unknown"
                )

            # 모든 컬럼이 범주형이라고 가정
            df[col] = df[col].astype(str)
            test = le.transform(df[col])
            df[col] = test

        return df

    def __feature_engineering(self, df):
        # TODO
        return df

    def load_data_from_file(self, file_name, is_train=True):
        csv_file_path = os.path.join(self.args.data_dir, file_name)
        df = pd.read_csv(csv_file_path)  # , nrows=100000)

        if is_train:
            print("\n[3-1] Only get train dataset")
            df = df[df["dataset"] == 1]
        else:
            print("\n[3-1] Only get test dataset")
            df = df[df["dataset"] == 2]

        print("\n[3-2] function: __feature_engineering (nothing..)")
        df = self.__feature_engineering(df)

        print("\n[3-3] function: __preprocessing (categorical feats -> grant labels)")
        df = self.__preprocessing(df, is_train)

        # 추후 feature를 embedding할 시에 embedding_layer의 input 크기를 결정할때 사용
        self.args.n_embdings = EasyDict()

        print("\n[3-4] load  *_classes.npy")
        for col_name in self.args.cate_feats:
            self.args.n_embdings[col_name] = len(
                np.load(os.path.join(self.args.asset_dir, col_name + "_classes.npy"))
            )

        df = df.sort_values(by=["userID", "Timestamp"], axis=0)

        # 필요 없는 columns 제외
        columns = [i for i in list(df) if i not in ["Timestamp", "dataset"]]

        group = (
            df[columns]
            .groupby("userID")
            .apply(lambda r: tuple(r[col].values for col in columns))
        )

        # columns position
        self.args.columns = {col_name: idx for idx, col_name in enumerate(columns)}

        # category feature location
        self.args.cate_loc = {
            col: i for i, col in enumerate(columns) if col in self.args.cate_feats
        }

        # category feature location
        self.args.conti_loc = {
            col: i for i, col in enumerate(columns) if col in self.args.conti_feats
        }

        return group.values

    def load_train_data(self, file_name):
        self.train_data = self.load_data_from_file(file_name)

    def load_test_data(self, file_name):
        self.test_data = self.load_data_from_file(file_name, is_train=False)


class DKTDataset(torch.utils.data.Dataset):
    def __init__(self, data, args):
        self.data = data
        self.args = args

    def __getitem__(self, index):
        row = self.data[index]

        # 각 data의 sequence length
        seq_len = len(row[0])
        # cate_cols = [test, question, tag, correct]
        feat_cols = list(row)

        # max seq len을 고려하여서 이보다 길면 자르고 아닐 경우 그대로 냅둔다
        if seq_len > self.args.max_seq_len:
            for i, col in enumerate(feat_cols):
                feat_cols[i] = col[-self.args.max_seq_len :]

            mask = np.ones(self.args.max_seq_len, dtype=np.int16)
        else:
            mask = np.zeros(self.args.max_seq_len, dtype=np.int16)
            mask[-seq_len:] = 1

        # mask도 columns 목록에 포함시킴
        feat_cols.append(mask)

        # np.array -> torch.tensor 형변환
        # cate -> torch.tensor
        # conti -> torch.FloatTensor
        for i, col in enumerate(feat_cols):
            if i in self.args.conti_loc.values():  # continus col index
                feat_cols[i] = torch.FloatTensor(col)
            else:
                feat_cols[i] = torch.tensor(col)

        return feat_cols

    def __len__(self):
        return len(self.data)


from torch.nn.utils.rnn import pad_sequence


def collate(batch):
    col_n = len(batch[0])
    col_list = [[] for _ in range(col_n)]
    max_seq_len = len(batch[0][-1])

    # batch의 값들을 각 column끼리 그룹화
    for row in batch:
        for i, col in enumerate(row):
            pre_padded = torch.zeros(max_seq_len)
            pre_padded[-len(col) :] = col
            col_list[i].append(pre_padded)

    for i, _ in enumerate(col_list):
        col_list[i] = torch.stack(col_list[i])

    return tuple(col_list)


def get_loaders(args, train, valid):

    pin_memory = False
    train_loader, valid_loader = None, None

    if train is not None:
        trainset = DKTDataset(train, args)
        train_loader = torch.utils.data.DataLoader(
            trainset,
            num_workers=args.num_workers,
            shuffle=True,
            batch_size=args.batch_size,
            pin_memory=pin_memory,
            collate_fn=collate,
        )
    if valid is not None:
        valset = DKTDataset(valid, args)
        valid_loader = torch.utils.data.DataLoader(
            valset,
            num_workers=args.num_workers,
            shuffle=False,
            batch_size=args.batch_size,
            pin_memory=pin_memory,
            collate_fn=collate,
        )

    return train_loader, valid_loader


## Copyed from Special mission
def slidding_window(data, args):
    window_size = args.max_seq_len
    stride = args.stride

    augmented_datas = []
    for row in data:
        seq_len = len(row[0])

        # 만약 window 크기보다 seq len이 같거나 작으면 augmentation을 하지 않는다
        if seq_len <= window_size:
            augmented_datas.append(row)
        else:
            total_window = ((seq_len - window_size) // stride) + 1

            # 앞에서부터 slidding window 적용
            for window_i in range(total_window):
                # window로 잘린 데이터를 모으는 리스트
                window_data = []
                for col in row:
                    window_data.append(
                        col[window_i * stride : window_i * stride + window_size]
                    )

                # Shuffle
                # 마지막 데이터의 경우 shuffle을 하지 않는다
                if args.shuffle and window_i + 1 != total_window:
                    shuffle_datas = shuffle(window_data, window_size, args)
                    augmented_datas += shuffle_datas
                else:
                    augmented_datas.append(tuple(window_data))

            # slidding window에서 뒷부분이 누락될 경우 추가
            total_len = window_size + (stride * (total_window - 1))
            if seq_len != total_len:
                window_data = []
                for col in row:
                    window_data.append(col[-window_size:])
                augmented_datas.append(tuple(window_data))

    return augmented_datas


def shuffle(data, data_size, args):
    shuffle_datas = []
    for i in range(args.shuffle_n):
        # shuffle 횟수만큼 window를 랜덤하게 계속 섞어서 데이터로 추가
        shuffle_data = []
        random_index = np.random.permutation(data_size)
        for col in data:
            shuffle_data.append(col[random_index])
        shuffle_datas.append(tuple(shuffle_data))
    return shuffle_datas


def data_augmentation(data, args):
    if args.window == True:
        print("\n[4-1] Do Sliding Window Augmentation")
        data = slidding_window(data, args)

    return data
