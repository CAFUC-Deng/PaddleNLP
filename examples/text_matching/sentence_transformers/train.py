# Copyright (c) 2020 PaddlePaddle Authors. All Rights Reserved.
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

from functools import partial
import argparse
import os
import random
import time

import numpy as np
import paddle
import paddle.nn.functional as F

import paddlenlp as ppnlp
from paddlenlp.data import Stack, Tuple, Pad
from paddlenlp.transformers import LinearDecayWithWarmup

from model import SentenceTransformer

# yapf: disable
parser = argparse.ArgumentParser()
parser.add_argument("--save_dir", default='./checkpoint', type=str, help="The output directory where the model checkpoints will be written.")
parser.add_argument("--max_seq_length", default=128, type=int, help="The maximum total input sequence length after tokenization. "
    "Sequences longer than this will be truncated, sequences shorter will be padded.")
parser.add_argument("--batch_size", default=32, type=int, help="Batch size per GPU/CPU for training.")
parser.add_argument("--learning_rate", default=5e-5, type=float, help="The initial learning rate for Adam.")
parser.add_argument("--weight_decay", default=0.0, type=float, help="Weight decay if we apply some.")
parser.add_argument("--epochs", default=3, type=int, help="Total number of training epochs to perform.")
parser.add_argument("--warmup_proption", default=0.0, type=float, help="Linear warmup proption over the training process.")
parser.add_argument("--init_from_ckpt", type=str, default=None, help="The path of checkpoint to be loaded.")
parser.add_argument("--seed", type=int, default=1000, help="random seed for initialization")
parser.add_argument("--n_gpu", type=int, default=1, help="Number of GPUs to use, 0 for CPU.")
args = parser.parse_args()
# yapf: enable


def set_seed(seed):
    """sets random seed"""
    random.seed(seed)
    np.random.seed(seed)
    paddle.seed(seed)


@paddle.no_grad()
def evaluate(model, criterion, metric, data_loader):
    """
    Given a dataset, it evals model and computes the metric.

    Args:
        model(obj:`paddle.nn.Layer`): A model to classify texts.
        data_loader(obj:`paddle.io.DataLoader`): The dataset loader which generates batches.
        criterion(obj:`paddle.nn.Layer`): It can compute the loss.
        metric(obj:`paddle.metric.Metric`): The evaluation metric.
    """
    model.eval()
    metric.reset()
    losses = []
    for batch in data_loader:
        query_input_ids, query_segment_ids, title_input_ids, title_segment_ids, labels = batch
        probs = model(
            query_input_ids=query_input_ids,
            title_input_ids=title_input_ids,
            query_token_type_ids=query_segment_ids,
            title_token_type_ids=title_segment_ids)
        loss = criterion(probs, labels)
        losses.append(loss.numpy())
        correct = metric.compute(probs, labels)
        metric.update(correct)
        accu = metric.accumulate()
    print("eval loss: %.5f, accu: %.5f" % (np.mean(losses), accu))
    model.train()
    metric.reset()


def convert_example(example,
                    tokenizer,
                    label_list,
                    max_seq_length=512,
                    is_test=False):
    """
    Builds model inputs from a sequence or a pair of sequence for sequence classification tasks
    by concatenating and adding special tokens. And creates a mask from the two sequences passed 
    to be used in a sequence-pair classification task.
        
    A BERT sequence has the following format:

    - single sequence: ``[CLS] X [SEP]``
    - pair of sequences: ``[CLS] A [SEP] B [SEP]``

    A BERT sequence pair mask has the following format:
    ::
        0 0 0 0 0 0 0 0 0 0 0 1 1 1 1 1 1 1 1 1
        | first sequence    | second sequence |

    If only one sequence, only returns the first portion of the mask (0's).


    Args:
        example(obj:`list[str]`): List of input data, containing query, title and label if it have label.
        tokenizer(obj:`PretrainedTokenizer`): This tokenizer inherits from :class:`~paddlenlp.transformers.PretrainedTokenizer` 
            which contains most of the methods. Users should refer to the superclass for more information regarding methods.
        label_list(obj:`list[str]`): All the labels that the data has.
        max_seq_len(obj:`int`): The maximum total input sequence length after tokenization. 
            Sequences longer than this will be truncated, sequences shorter will be padded.
        is_test(obj:`False`, defaults to `False`): Whether the example contains label or not.

    Returns:
        query_input_ids(obj:`list[int]`): The list of query token ids.
        query_segment_ids(obj: `list[int]`): List of query sequence pair mask.
        title_input_ids(obj:`list[int]`): The list of title token ids.
        title_segment_ids(obj: `list[int]`): List of title sequence pair mask.
        label(obj:`numpy.array`, data type of int64, optional): The input label if not is_test.
    """
    query, title = example[0], example[1]

    query_encoded_inputs = tokenizer.encode(
        text=query, max_seq_len=max_seq_length)
    query_input_ids = query_encoded_inputs["input_ids"]
    query_segment_ids = query_encoded_inputs["segment_ids"]

    title_encoded_inputs = tokenizer.encode(
        text=title, max_seq_len=max_seq_length)
    title_input_ids = title_encoded_inputs["input_ids"]
    title_segment_ids = title_encoded_inputs["segment_ids"]

    if not is_test:
        # create label maps if classification task
        label = example[-1]
        label_map = {}
        for (i, l) in enumerate(label_list):
            label_map[l] = i
        label = label_map[label]
        label = np.array([label], dtype="int64")
        return query_input_ids, query_segment_ids, title_input_ids, title_segment_ids, label
    else:
        return query_input_ids, query_segment_ids, title_input_ids, title_segment_ids


def create_dataloader(dataset,
                      mode='train',
                      batch_size=1,
                      batchify_fn=None,
                      trans_fn=None):
    if trans_fn:
        dataset = dataset.apply(trans_fn, lazy=True)

    shuffle = True if mode == 'train' else False
    if mode == 'train':
        batch_sampler = paddle.io.DistributedBatchSampler(
            dataset, batch_size=batch_size, shuffle=shuffle)
    else:
        batch_sampler = paddle.io.BatchSampler(
            dataset, batch_size=batch_size, shuffle=shuffle)

    return paddle.io.DataLoader(
        dataset=dataset,
        batch_sampler=batch_sampler,
        collate_fn=batchify_fn,
        return_list=True)


def do_train():
    set_seed(args.seed)
    paddle.set_device("gpu" if args.n_gpu else "cpu")
    world_size = paddle.distributed.get_world_size()
    if world_size > 1:
        paddle.distributed.init_parallel_env()

    train_dataset, dev_dataset, test_dataset = ppnlp.datasets.LCQMC.get_datasets(
        ['train', 'dev', 'test'])

    # If you wanna use bert/roberta pretrained model,
    # pretrained_model = ppnlp.transformers.BertModel.from_pretrained('bert-base-chinese') 
    # pretrained_model = ppnlp.transformers.RobertaModel.from_pretrained('roberta-wwm-ext')
    pretrained_model = ppnlp.transformers.ErnieModel.from_pretrained(
        'ernie-tiny')

    # If you wanna use bert/roberta pretrained model,
    # tokenizer = ppnlp.transformers.BertTokenizer.from_pretrained('bert-base-chinese')
    # tokenizer = ppnlp.transformers.RobertaTokenizer.from_pretrained('roberta-wwm-ext')
    # ErnieTinyTokenizer is special for ernie-tiny pretained model.
    tokenizer = ppnlp.transformers.ErnieTinyTokenizer.from_pretrained(
        'ernie-tiny')

    trans_func = partial(
        convert_example,
        tokenizer=tokenizer,
        label_list=train_dataset.get_labels(),
        max_seq_length=args.max_seq_length)
    batchify_fn = lambda samples, fn=Tuple(
        Pad(axis=0, pad_val=tokenizer.pad_token_id),  # query_input
        Pad(axis=0, pad_val=tokenizer.pad_token_id),  # query_segment
        Pad(axis=0, pad_val=tokenizer.pad_token_id),  # title_input
        Pad(axis=0, pad_val=tokenizer.pad_token_id),  # tilte_segment
        Stack(dtype="int64")  # label
    ): [data for data in fn(samples)]
    train_data_loader = create_dataloader(
        train_dataset,
        mode='train',
        batch_size=args.batch_size,
        batchify_fn=batchify_fn,
        trans_fn=trans_func)
    dev_data_loader = create_dataloader(
        dev_dataset,
        mode='dev',
        batch_size=args.batch_size,
        batchify_fn=batchify_fn,
        trans_fn=trans_func)
    test_data_loader = create_dataloader(
        test_dataset,
        mode='test',
        batch_size=args.batch_size,
        batchify_fn=batchify_fn,
        trans_fn=trans_func)

    model = SentenceTransformer(pretrained_model)

    if args.init_from_ckpt and os.path.isfile(args.init_from_ckpt):
        state_dict = paddle.load(args.init_from_ckpt)
        model.set_dict(state_dict)
    model = paddle.DataParallel(model)

    num_training_steps = len(train_data_loader) * args.epochs

    lr_scheduler = LinearDecayWithWarmup(args.learning_rate, num_training_steps,
                                         args.warmup_proportion)

    optimizer = paddle.optimizer.AdamW(
        learning_rate=lr_scheduler,
        parameters=model.parameters(),
        weight_decay=args.weight_decay,
        apply_decay_param_fun=lambda x: x in [
            p.name for n, p in model.named_parameters()
            if not any(nd in n for nd in ["bias", "norm"])
        ])

    criterion = paddle.nn.loss.CrossEntropyLoss()
    metric = paddle.metric.Accuracy()

    global_step = 0
    tic_train = time.time()
    for epoch in range(1, args.epochs + 1):
        for step, batch in enumerate(train_data_loader, start=1):
            query_input_ids, query_segment_ids, title_input_ids, title_segment_ids, labels = batch
            probs = model(
                query_input_ids=query_input_ids,
                title_input_ids=title_input_ids,
                query_token_type_ids=query_segment_ids,
                title_token_type_ids=title_segment_ids)
            loss = criterion(probs, labels)
            correct = metric.compute(probs, labels)
            metric.update(correct)
            acc = metric.accumulate()

            global_step += 1
            if global_step % 10 == 0 and paddle.distributed.get_rank() == 0:
                print(
                    "global step %d, epoch: %d, batch: %d, loss: %.5f, accu: %.5f, speed: %.2f step/s"
                    % (global_step, epoch, step, loss, acc,
                       10 / (time.time() - tic_train)))
                tic_train = time.time()
            loss.backward()
            optimizer.step()
            lr_scheduler.step()
            optimizer.clear_gradients()
            if global_step % 100 == 0 and paddle.distributed.get_rank() == 0:
                save_dir = os.path.join(args.save_dir, "model_%d" % global_step)
                if not os.path.exists(save_dir):
                    os.makedirs(save_dir)
                evaluate(model, criterion, metric, dev_data_loader)
                save_param_path = os.path.join(save_dir, 'model_state.pdparams')
                paddle.save(model.state_dict(), save_param_path)
                tokenizer.save_pretrained(save_dir)

    if paddle.distributed.get_rank() == 0:
        print('Evaluating on test data.')
        evaluate(model, criterion, metric, test_data_loader)


if __name__ == "__main__":
    if args.n_gpu > 1:
        paddle.distributed.spawn(do_train, nprocs=args.n_gpu)
    else:
        do_train()