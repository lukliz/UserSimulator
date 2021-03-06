# coding: utf-8
from __future__ import print_function, division
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torch.autograd import Variable
import os
import errno
import re
import time
import tempfile
import codecs
import json
import subprocess
import random
import argparse
import logging
import numpy as np
import sys

from nn_models import MultiLableClassifyLayer, LSTM_MultiLabelClassifier, Seq2SeqActionGenerator, State2Seq
from prepare_data import *

try:
    from deep_dialog import dialog_config
except:
    sys.path.append("..")
    import dialog_config

    sys.path.append("../../")
    from run_action_generation import main

logging.basicConfig(filename='', format='%(asctime)-15s %(levelname)s: %(message)s', level=logging.INFO)

# PROJECT_DIR = 'E:/Projects/Research/'  # for Windows
PROJECT_DIR = '/users4/ythou/Projects/'  # for hpc
# PROJECT_DIR = 'E:/Projects/Research/'  # for tencent linux

# DATA_MARK = 'extracted_no_nlg_no_nlu_lstm '
DATA_MARK = 'extracted_no_nlg_no_nlu'


HARD_CODED_V_COMPONENT = [
    'goal_inform_slots_v',
    'goal_request_slots_v',
    'history_slots_v',  # informed slots, 1 informed, 0 irrelevant, -1 not informed,
    'rest_slots_v',  # remained request slots, 1 for remained, 0 irrelevant, -1 for already got,
    'system_diaact_v',  # system diaact,
    'system_inform_slots_v',  # inform slots of sys response,
    'system_request_slots_v',  # request slots of sys response,
    'consistency_v',  # for each position, -1 inconsistent, 0 irrelevent or not requested, 1 consistent,
    'dialog_status_v'  # -1, 0, 1 for failed, no outcome, success,
]

USE_TEACHER_FORCING_LIST = ['ssg', 'seq2seq_gen', 'ssag', 'seq2seq_att_gen', 'sv2s', 'state_v2seq']

debug_str = '======================= DEBUG ========================'


def get_f1(pred_tags_lst, golden_tags_lst):
    """
    get sentence level f score
    :param pred_tags_lst: list of one hot alike tags i.e. [[1, 0, 1]]
    :param golden_tags_lst: list of one hot alike tags i.e. [[1, 0, 1]]
    :return: precision, recall, f1
    """
    tp, fp, fn = 0, 0, 0
    # print(len(pred_tags_lst), len(pred_tags_lst[0]), golden_tags_lst.shape)
    for pred_tags, golden_tags in zip(pred_tags_lst, golden_tags_lst):
        if len(pred_tags) != len(golden_tags):
            logging.error('Unmatched tags: \npred:{}{}\ngold:{}{}'.format(
                len(pred_tags), pred_tags, len(golden_tags), golden_tags)
            )
            raise RuntimeError
        for pred_t, gold_t in zip(pred_tags, golden_tags):
            if pred_t == 1:
                if pred_t == gold_t:
                    tp += 1
                elif gold_t == 0:
                    fp += 1
                else:
                    raise RuntimeError
            elif pred_t == 0:
                if pred_t == gold_t:
                    pass
                elif gold_t == 1:
                    fn += 1
                else:
                    raise RuntimeError
    if tp == 0:
        precision = 0
        recall = 0
        f1 = 0
    else:
        precision = 1.0 * tp / (tp + fp)
        recall = 1.0 * tp / (tp + fn)
        f1 = 2 * precision * recall / (precision + recall)
    return precision, recall, f1


def valid_value_check(pred_type, pred_value, full_dict):
    """ to avoid the case: predict slot as diaact and vice versa."""
    if pred_type == 'diaact':
        return pred_value in full_dict['diaact2id']
    elif pred_type == 'inform_slots':
        return pred_value in full_dict['user_inform_slot2id']
    elif pred_type == 'request_slots':
        return pred_value in full_dict['user_request_slot2id']
    else:
        raise RuntimeError("Wong pred_type")


def gen2vector(pred, id2token, full_dict):
    tokens = [id2token[int(i)] for i in pred]
    pred_dict = {
        'diaact': [],
        'inform_slots': [],
        'request_slots': []
    }
    bad_slot = ['<PAD>', '<EOS>', '<SOS>'] + pred_dict.keys()
    for ind, token in enumerate(tokens):
        if token in pred_dict and ind + 1 < len(tokens) and tokens[ind + 1] not in bad_slot:
            if valid_value_check(pred_type=token, pred_value=tokens[ind + 1], full_dict=full_dict):
                pred_dict[token].append(tokens[ind + 1])
    # print('========= DEBUG ========== pred_dict', pred_dict)
    label = create_one_hot_v(pred_dict['diaact'], full_dict['diaact2id']) + \
        create_one_hot_v(pred_dict['inform_slots'], full_dict['user_inform_slot2id']) + \
        create_one_hot_v(pred_dict['request_slots'], full_dict['user_request_slot2id'])
    return label


def get_f1_from_generation(pred_tags_lst, golden_tags_lst, full_dict):
    id2token = full_dict['tgt_id2token']
    # print('========== pred ===========')
    pred_tags_lst = [gen2vector(tags, id2token, full_dict) for tags in pred_tags_lst]
    # print('===========  gold ============')
    golden_tags_lst = [gen2vector(tags, id2token, full_dict) for tags in golden_tags_lst]
    return get_f1(pred_tags_lst, golden_tags_lst)


def vector2state(state_vector, full_dict):
    state_v_component = dialog_config.STATE_V_COMPONENT

    if state_v_component != HARD_CODED_V_COMPONENT:
        raise RuntimeError("vector2state is hard coded, please alter the vector_corresponding_dict_lst")

    id2sys_inform_slot = full_dict['id2sys_inform_slot']
    id2sys_request_slot = full_dict['id2sys_request_slot']
    id2user_inform_slot = full_dict['id2user_inform_slot']
    id2user_request_slot = full_dict['id2user_request_slot']
    id2diaact = full_dict['id2diaact']

    vector_corresponding_dict_lst = [
        id2user_inform_slot, id2user_request_slot, id2user_inform_slot, id2user_request_slot,
        id2diaact, id2sys_inform_slot, id2sys_request_slot, id2user_inform_slot,
        {-1: 'fail', 0: 'no_outcome', 1: 'success'}
    ]
    ret = {}
    start_idx = 0

    state_vector_dict = {}
    state_vector_list = []
    user_goal = {}
    sys_action = {}  # request slots, inform slots, diaact, speaker
    history_slots = {}  # pos: informed, neg: not_informed, irr: irrelevant
    rest_slots = {}  # pos: remained, neg: already_got, irrelevant
    consistent_slots = {}  # pos: consistent, neg: inconsistent, irrelevant
    dialog_status = ''

    def decode_3_value_vector(vector, id2item):
        pos = []
        neg = []
        irr = []
        for ind, value in enumerate(vector):
            ind = str(ind)
            if value == 1:
                pos.append(id2item[ind])
            elif value == -1:
                neg.append(id2item[ind])
            elif value == 0:
                irr.append(id2item[ind])
            else:
                raise RuntimeError('Error Value: {}'.format(value))
        return {'pos': pos, 'neg': neg, 'irr': irr}
    
    def decode_2_value_vector(vector, id2item):
        pos = []
        irr = []
        for ind, value in enumerate(vector):
            ind = str(ind)
            if value == 1:
                pos.append(id2item[ind])
            elif value == 0:
                irr.append(id2item[ind])
            else:
                raise RuntimeError('Error Value: {}'.format(value))
        return {'pos': pos, 'irr': irr}

    def decode_1_value_vector(vector, id2item):
        return id2item[vector[0]]

    for name, id2item in zip(state_v_component, vector_corresponding_dict_lst):
        component_v = state_vector[start_idx: start_idx + len(id2item)]
        state_vector_dict[name] = component_v
        state_vector_list.append(component_v)
        if name == 'goal_inform_slots_v':
            tmp = decode_2_value_vector(component_v, id2item)
            user_goal['inform_slots'] = tmp['pos']

        if name == 'goal_request_slots_v':
            tmp = decode_2_value_vector(component_v, id2item)
            user_goal['request_slots'] = tmp['pos']

        if name == 'history_slots_v':  # informed slots, 1 informed, 0 irrelevant, -1 not informed
            tmp = decode_3_value_vector(component_v, id2item)
            history_slots['pos'] = tmp['pos']
            history_slots['neg'] = tmp['neg']

        if name == 'rest_slots_v':  # remained request slots, 1 for remained, 0 irrelevant, -1 for already got
            tmp = decode_3_value_vector(component_v, id2item)
            rest_slots['pos'] = tmp['pos']
            rest_slots['neg'] = tmp['neg']

        if name == 'system_diaact_v':  # system diaact
            tmp = decode_2_value_vector(component_v, id2item)
            sys_action['diaact'] = tmp

        if name == 'system_inform_slots_v':  # inform slots of sys response
            tmp = decode_2_value_vector(component_v, id2item)
            sys_action['inform_slots'] = tmp['pos']

        if name == 'system_request_slots_v':  # request slots of sys response
            tmp = decode_2_value_vector(component_v, id2item)
            sys_action['request_slots'] = tmp['pos']

        if name == 'consistency_v':  # for each position, -1 inconsistent, 0 irrelevent or not requested, 1 consistent
            tmp = decode_3_value_vector(component_v, id2item)
            consistent_slots['pos'] = tmp['pos']
            consistent_slots['neg'] = tmp['neg']

        if name == 'dialog_status_v':  # -1, 0, 1 for failed, no outcome, success,
            tmp = decode_1_value_vector(component_v, id2item)
            dialog_status = tmp
        start_idx += len(id2item)
    ret = {
        'state_vector_dict': state_vector_dict,
        'state_vector_list': state_vector_list,
        'user_goal': user_goal,
        'sys_action': sys_action,  # request slots, inform slots, diaact, speaker
        'history_slots': history_slots,  # pos: informed, neg: not_informed, irr: irrelevant
        'rest_slots': rest_slots,  # pos: remained, neg: already_got, irrelevant
        'consistent_slots': consistent_slots,  # pos: consistent, neg: inconsistent, irrelevant
        'dialog_status': dialog_status,
    }
    return ret


def vector2action(action_vector, full_dict):
    id2diaact = full_dict['id2diaact']
    id2user_inform_slot = full_dict['id2user_inform_slot']
    id2user_request_slot = full_dict['id2user_request_slot']
    diaact_end = len(id2diaact)
    inform_slot_end = len(id2user_request_slot) + diaact_end
    diaact_v = action_vector[: diaact_end]
    inform_slots_v = action_vector[diaact_end: inform_slot_end]
    request_slots_v = action_vector[inform_slot_end:]

    diaact = ''
    inform_slots = []
    request_slots = []
    for ind, item in enumerate(diaact_v):
        if item == 1:
            if not diaact:
                diaact = id2diaact[str(ind)]
            else:
                print('Warning: multi-action predicted')
                # print(len(action_vector), action_vector, diaact_end, id2diaact)
                # raise RuntimeError
    for ind, item in enumerate(inform_slots_v):
        if item == 1:
            inform_slots.append(id2user_inform_slot[str(ind)])
    for ind, item in enumerate(request_slots_v):
        if item == 1:
            request_slots.append(id2user_request_slot[str(ind)])
    # if not diaact:
    #   print('====== DEBUG ======', action_vector, id2diaact)
    ret = {
        'diaact': diaact,
        'inform_slots': inform_slots,
        'request_slots': request_slots,
    }
    return ret


def eval_model(model, valid_x, valid_y, full_dict, opt):

    if opt.output is not None:
        output_path = opt.output
        output_file = open(output_path, 'w')
    else:
        output_file = None
    model.eval()

    all_preds = []
    for x, y in zip(valid_x, valid_y):
        output = model.forward(x, y)
        output_data = output
        # for s, pred_a, gold_a in zip(x, output_data, y):
            # log = {
            #     'state': vector2state(s.tolist(), id2label),
            #     'pred': vector2action(pred_a, id2label),
            #     'gold': vector2action(gold_a, id2label),
            # }
            # if output_file:
            #     output_file.write(json.dumps(log))
        all_preds.extend(output_data)

    output_file.close()
    if type(valid_y) == list:
        tmp = []
        for yi in valid_y:
            tmp.extend(yi)
        valid_y = tmp
    else:
        valid_y = torch.cat(valid_y)  # re-form batches into one
    if opt.select_model in USE_TEACHER_FORCING_LIST:
        precision, recall, f1 = get_f1_from_generation(pred_tags_lst=all_preds, golden_tags_lst=valid_y, full_dict=full_dict)
    else:
        precision, recall, f1 = get_f1(pred_tags_lst=all_preds, golden_tags_lst=valid_y)
    return precision, recall, f1


def train_model(epoch, model, optimizer,
                train_x, train_y,
                valid_x, valid_y,
                test_x, test_y,
                full_dict, best_valid, test_f1_score):
    model.train()
    opt = model.opt

    total_loss = 0.0
    cnt = 0
    start_time = time.time()

    lst = list(range(len(train_x)))
    random.shuffle(lst)
    train_x = [train_x[l] for l in lst]
    train_y = [train_y[l] for l in lst]

    for x, y in zip(train_x, train_y):
        cnt += 1
        model.zero_grad()
        if opt.select_model in USE_TEACHER_FORCING_LIST:
            _, loss = model.forward(x, y, teacher_forcing_ratio=opt.teacher_forcing_ratio)
            # print('----------- debug in train part ------------', opt.teacher_forcing_ratio)
        else:
            _, loss = model.forward(x, y)
        total_loss += loss.data[0]
        n_tags = len(train_y[0]) * len(x)
        loss.backward()
        torch.nn.utils.clip_grad_norm(model.parameters(), opt.clip_grad)
        optimizer.step()
        if cnt * opt.batch_size % 1024 == 0:
            logging.info("Epoch={} iter={} lr={:.6f} train_ave_loss={:.6f} time={:.2f}s".format(
                epoch, cnt, optimizer.param_groups[0]['lr'],
                1.0 * loss.data[0] / n_tags, time.time() - start_time
            ))
            start_time = time.time()

    dev_precision, dev_recall, dev_f1_score = eval_model(model, valid_x, valid_y, full_dict, opt)
    logging.info("Epoch={} iter={} lr={:.6f} train_loss={:.6f} valid_f1={:.6f} valid_p={:.6f} valid_r={:.6f}".format(
        epoch, cnt, optimizer.param_groups[0]['lr'], total_loss, dev_f1_score, dev_precision, dev_recall))

    if dev_f1_score > best_valid:
        saving_dict = {
            'state_dict': model.state_dict(),
            'param':
            {
                'input_size': model.input_size,
                'hidden_size': model.hidden_size,
                'opt': model.opt,
                'use_cuda': model.use_cuda
            }
        }

        if hasattr(model, 'num_tags'):
            saving_dict['param']['num_tags'] = model.num_tags
        ''' Store param for seq2seq model: sos_id, eos_id, token2id, id2token '''
        if hasattr(model, 'tgt_vocb_size'):
            saving_dict['param']['tgt_vocb_size'] = model.tgt_vocb_size
        if hasattr(model, 'sos_id'):
            saving_dict['param']['sos_id'] = model.sos_id
        if hasattr(model, 'eos_id'):
            saving_dict['param']['eos_id'] = model.eos_id
        if hasattr(model, 'token2id'):
            saving_dict['param']['token2id'] = model.token2id
        if hasattr(model, 'id2token'):
            saving_dict['param']['id2token'] = model.id2token
        ''' Store Param For state2seq model: slot_num, diaact_num, embedded_v_size, state_v_component'''
        if hasattr(model, 'slot_num'):
            saving_dict['param']['slot_num'] = model.slot_num
        if hasattr(model, 'diaact_num'):
            saving_dict['param']['diaact_num'] = model.diaact_num
        if hasattr(model, 'embedded_v_size'):
            saving_dict['param']['embedded_v_size'] = model.embedded_v_size
        if hasattr(model, 'state_v_component'):
            saving_dict['param']['state_v_component'] = model.state_v_component

        torch.save(
            saving_dict,
            os.path.join(opt.model, opt.model_name)
        )
        best_valid = dev_f1_score
        test_precision, test_recall, test_f1_score = eval_model(model, test_x, test_y, full_dict, opt)
        logging.info("New record achieved!")
        logging.info("Epoch={} iter={} lr={:.6f} test_precision={:.6f}, test_recall={:.6f}, test_f1={:.6f}".format(
            epoch, cnt, optimizer.param_groups[0]['lr'], test_precision, test_recall, test_f1_score))
    return best_valid, test_f1_score


def create_one_batch(x, y, use_cuda=False, tensor=True):
    batch_size = len(x)
    lens = [len(xi) for xi in x]
    max_len = max(lens)  # for variable length situation for current situation

    if tensor:
        batch_x = torch.LongTensor(x)
        batch_y = torch.LongTensor(y)
        if use_cuda:
            batch_x = batch_x.cuda()
            batch_y = batch_y.cuda()
    else:
        batch_x = x
        batch_y = y
    return batch_x, batch_y, lens


def create_batches(x, y, batch_size, sort=True, shuffle=True, use_cuda=False, tensor=True):
    lst = list(range(len(x)))
    if shuffle:
        random.shuffle(lst)
    if sort:
        lst = sorted(lst, key=lambda i: -len(x[i]))

    x = [x[i] for i in lst]
    y = [y[i] for i in lst]

    nbatch = (len(x) - 1) // batch_size + 1  # subtract 1 fist to handle situation: len(x) // batch_size == 0
    batches_x, batches_y = [], []

    for i in range(nbatch):
        start_id, end_id = i * batch_size, (i + 1) * batch_size
        bx, by, _ = create_one_batch(x[start_id: end_id], y[start_id: end_id], use_cuda, tensor)

        batches_x.append(bx)
        batches_y.append(by)

    if sort:
        pos_lst = list(range(nbatch))
        random.shuffle(pos_lst)

        batches_x = [batches_x[i] for i in pos_lst]
        batches_y = [batches_y[i] for i in pos_lst]

    logging.info("{} batches, batch size: {}".format(nbatch, batch_size))
    return batches_x, batches_y


def one_turn_classification(opt):
    use_cuda = opt.gpu >= 0 and torch.cuda.is_available()
    with open(opt.train_path, 'r') as train_file, \
            open(opt.dev_path, 'r') as dev_file, \
            open(opt.test_path, 'r') as test_file, \
            open(opt.dict_path, 'r') as dict_file:
        logging.info('Start loading data from:\ntrain:{}\ndev:{}\ntest:{}\ndict:{}\n'.format(
            opt.train_path, opt.dev_path, opt.test_path, opt.dict_path
        ))
        train_data = json.load(train_file)
        dev_data = json.load(dev_file)
        test_data = json.load(test_file)
        full_dict = json.load(dict_file)

        # TODO: change full dict to a whole dict
        logging.info('Finish data loading.')
        print('Finish  data loading!!!!!!!!!!')
        # unpack data
        train_input, train_label, train_turn_id = zip(* train_data)
        dev_input, dev_label, dev_turn_id = zip(* dev_data)
        test_input, test_label, test_turn_id = zip(* test_data)
        train_x, train_y = create_batches(train_input, train_label, opt.batch_size, use_cuda=use_cuda)
        dev_x, dev_y = create_batches(dev_input, dev_label, opt.batch_size, use_cuda=use_cuda)
        test_x, test_y = create_batches(test_input, test_label, opt.batch_size, use_cuda=use_cuda)

        input_size = len(train_input[0])
        num_tags = len(train_label[0])
        classifier = MultiLableClassifyLayer(input_size=input_size, hidden_size=opt.hidden_dim, num_tags=num_tags,
                                             opt=opt, use_cuda=use_cuda)

        optimizer = optim.Adam(classifier.parameters(), lr=opt.lr)

        best_valid, test_result = -1e8, -1e8
        for epoch in range(opt.max_epoch):
            best_valid, test_result = train_model(
                epoch=epoch,
                model=classifier,
                optimizer=optimizer,
                train_x=train_x, train_y=train_y,
                valid_x=dev_x, valid_y=dev_y,
                test_x=test_x, test_y=test_y,
                full_dict=full_dict, best_valid=best_valid, test_f1_score=test_result
            )
            if opt.lr_decay > 0:
                optimizer.param_groups[0]['lr'] *= opt.lr_decay  # there is only one group, so use index 0
            # logging.info('Total encoder time: {:.2f}s'.format(model.eval_time / (epoch + 1)))
            # logging.info('Total embedding time: {:.2f}s'.format(model.emb_time / (epoch + 1)))
            # logging.info('Total classify time: {:.2f}s'.format(model.classify_time / (epoch + 1)))
        logging.info("best_valid_f1: {:.6f}".format(best_valid))
        logging.info("test_f1: {:.6f}".format(test_result))


def transform_data_into_history_style(data, turn_ids, history_turns=5):
    ret = []
    history = []
    for item, turn_id in zip(data, turn_ids):
        if turn_id == 0:
            history = [item] * (history_turns - 1)  # pad empty history
        elif len(history) < 4:
            history = [item] * (history_turns - 1)  # deal with in-complete dialogue
        history.append(item)  # add current state vector
        sample = history[-history_turns:]
        ret.append(sample)
    return ret


def history_based_classification(opt):
    use_cuda = opt.gpu >= 0 and torch.cuda.is_available()
    with open(opt.train_path, 'r') as train_file, \
            open(opt.dev_path, 'r') as dev_file, \
            open(opt.test_path, 'r') as test_file, \
            open(opt.dict_path, 'r') as dict_file:
        logging.info('Start loading data from:\ntrain:{}\ndev:{}\ntest:{}\ndict:{}\n'.format(
            opt.train_path, opt.dev_path, opt.test_path, opt.dict_path
        ))
        train_data = json.load(train_file)
        dev_data = json.load(dev_file)
        test_data = json.load(test_file)
        full_dict = json.load(dict_file)

        # TODO: change full dict to a whole dict
        logging.info('Finish data loading.')
        print('Finish  data loading!!!!!!!!!!')
        # unpack data
        train_input, train_label, train_turn_id = zip(*train_data)
        dev_input, dev_label, dev_turn_id = zip(*dev_data)
        test_input, test_label, test_turn_id = zip(*test_data)

        # stack history
        train_input = transform_data_into_history_style(train_input, train_turn_id)
        dev_input = transform_data_into_history_style(dev_input, dev_turn_id)
        test_input = transform_data_into_history_style(test_input, test_turn_id)

        train_x, train_y = create_batches(train_input, train_label, opt.batch_size, use_cuda=use_cuda)
        dev_x, dev_y = create_batches(dev_input, dev_label, opt.batch_size, use_cuda=use_cuda)
        test_x, test_y = create_batches(test_input, test_label, opt.batch_size, use_cuda=use_cuda)

        input_size = len(train_input[0][0])
        num_tags = len(train_label[0])
        classifier = LSTM_MultiLabelClassifier(
            input_size=input_size, hidden_size=opt.hidden_dim, num_tags=num_tags,
            opt=opt, use_cuda=use_cuda
        )
        # classifier = LSTM_MultiLabelClassifier()
        optimizer = optim.Adam(classifier.parameters(), lr=opt.lr)

        best_valid, test_result = -1e8, -1e8
        for epoch in range(opt.max_epoch):
            best_valid, test_result = train_model(
                epoch=epoch,
                model=classifier,
                optimizer=optimizer,
                train_x=train_x, train_y=train_y,
                valid_x=dev_x, valid_y=dev_y,
                test_x=test_x, test_y=test_y,
                full_dict=full_dict, best_valid=best_valid, test_f1_score=test_result
            )
            if opt.lr_decay > 0:
                optimizer.param_groups[0]['lr'] *= opt.lr_decay  # there is only one group, so use index 0
            # logging.info('Total encoder time: {:.2f}s'.format(model.eval_time / (epoch + 1)))
            # logging.info('Total embedding time: {:.2f}s'.format(model.emb_time / (epoch + 1)))
            # logging.info('Total classify time: {:.2f}s'.format(model.classify_time / (epoch + 1)))
        logging.info("best_valid_f1: {:.6f}".format(best_valid))
        logging.info("test_f1: {:.6f}".format(test_result))


def transform_label_into_sequence_style(labels, full_dict, sos_token, eos_token, pad_token):
    ret = []
    token_order = ['diaact', 'inform_slots', 'request_slots']
    tgt_token2id = full_dict['tgt_token2id']

    def slot2tokens(k, v):
        tokens = []
        if type(v) == list:
            for value in v:
                tokens.append(k)
                tokens.append(value)
        else:
            tokens = [k, v]
        return tokens

    def tokens2ids(tokens, tgt_token2id):
        return [tgt_token2id[x] for x in tokens]
    #
    # def ids2one_hots(ids):
    #     ret = []
    #     for id in ids:
    #         tmp_v = [0 for i in range(len(tgt_token2id))]
    #         tmp_v[id] = 1
    #         ret.append(ret)
    #     return ret

    def append_pad(all_labels):
        ret = []
        max_len = max([len(label) for label in all_labels])
        for label in all_labels:
            label.extend([pad_token for i in range(max_len - len(label))])
            ret.append(label)
        return ret

    all_action_token = []
    for label_v in labels:
        action_dict = vector2action(label_v, full_dict)
        # print('=======DEBUG======= action dict: {}'.format(action_dict))
        action_tokens = [sos_token]
        for token_type in token_order:
            # get and pad token
            action_tokens.extend(slot2tokens(token_type, action_dict[token_type]))
        action_tokens.append(eos_token)
        all_action_token.append(action_tokens)

    all_action_token = append_pad(all_action_token)
    # print('========== DEBUG ========= all_action_token', all_action_token[:3])
    for action_tokens in all_action_token:
        # print('=======DEBUG======= action_tokens: {}'.format(action_tokens))
        action_token_ids = tokens2ids(action_tokens, tgt_token2id)
        # action_token_vectors = ids2one_hots(action_token_ids)
        ret.append(action_token_ids)
    # print('========== DEBUG ========= all_action_token_id', ret[:3])
    return ret


def seq2seq_action_generation(opt, single_turn_history=False):
    use_cuda = opt.gpu >= 0 and torch.cuda.is_available()
    with open(opt.train_path, 'r') as train_file, \
            open(opt.dev_path, 'r') as dev_file, \
            open(opt.test_path, 'r') as test_file, \
            open(opt.dict_path, 'r') as dict_file:
        logging.info('Start loading data from:\ntrain:{}\ndev:{}\ntest:{}\ndict:{}\n'.format(
            opt.train_path, opt.dev_path, opt.test_path, opt.dict_path
        ))
        train_data = json.load(train_file)
        dev_data = json.load(dev_file)
        test_data = json.load(test_file)
        full_dict = json.load(dict_file)

        logging.info('Finish data loading.')
        print('Finish  data loading!!!!!!!!!!')

        # unpack data
        train_input, train_label, train_turn_id = zip(*train_data)
        dev_input, dev_label, dev_turn_id = zip(*dev_data)
        test_input, test_label, test_turn_id = zip(*test_data)

        # stack history
        if not single_turn_history:
            train_input = transform_data_into_history_style(train_input, train_turn_id)
            dev_input = transform_data_into_history_style(dev_input, dev_turn_id)
            test_input = transform_data_into_history_style(test_input, test_turn_id)

        # gen tgt dict
        sos_token = '<SOS>'
        eos_token = '<EOS>'
        pad_token = '<PAD>'
        tgt_token2id_dict = {}
        tgt_id2token_dict = {}
        for token in (
            [pad_token, sos_token, eos_token, 'diaact', 'inform_slots', 'request_slots'] +
            full_dict['user_inform_slot2id'].keys() +
            full_dict['user_request_slot2id'].keys() +
            full_dict['diaact2id'].keys()
        ):
            if token not in tgt_token2id_dict:
                t_id = len(tgt_token2id_dict)
                tgt_token2id_dict[token] = t_id
                tgt_id2token_dict[t_id] = token
        full_dict['tgt_token2id'] = tgt_token2id_dict
        full_dict['tgt_id2token'] = tgt_id2token_dict

        # convert label to sequence
        train_label = transform_label_into_sequence_style(train_label, full_dict, sos_token, eos_token, pad_token)
        dev_label = transform_label_into_sequence_style(dev_label, full_dict, sos_token, eos_token, pad_token)
        test_label = transform_label_into_sequence_style(test_label, full_dict, sos_token, eos_token, pad_token)

        # create batches
        train_x, train_y = create_batches(train_input, train_label, opt.batch_size, use_cuda=use_cuda)
        dev_x, dev_y = create_batches(dev_input, dev_label, opt.batch_size, use_cuda=use_cuda)
        test_x, test_y = create_batches(test_input, test_label, opt.batch_size, use_cuda=use_cuda)
        #
        # for i in range(3):
        #     print('========= DEBUG =========', train_x[0][i])

        input_size = len(train_input[0][0])

        classifier = Seq2SeqActionGenerator(
            input_size=input_size, hidden_size=opt.hidden_dim, n_layers=opt.depth,
            tgt_vocb_size=len(tgt_token2id_dict), max_len=opt.max_len, dropout_p=opt.dropout,
            sos_id=tgt_token2id_dict[sos_token], eos_id=tgt_token2id_dict[eos_token],
            token2id=tgt_token2id_dict, id2token=tgt_id2token_dict, opt=opt,
            bidirectional=opt.direction == 'bi', use_attention=opt.use_attention, input_variable_lengths=False,
            use_cuda=use_cuda
        )

        optimizer = optim.Adam(classifier.parameters(), lr=opt.lr)

        best_valid, test_result = -1e8, -1e8
        for epoch in range(opt.max_epoch):
            best_valid, test_result = train_model(
                epoch=epoch,
                model=classifier,
                optimizer=optimizer,
                train_x=train_x, train_y=train_y,
                valid_x=dev_x, valid_y=dev_y,
                test_x=test_x, test_y=test_y,
                full_dict=full_dict, best_valid=best_valid, test_f1_score=test_result
            )
            if opt.lr_decay > 0:
                optimizer.param_groups[0]['lr'] *= opt.lr_decay  # there is only one group, so use index 0
            # logging.info('Total encoder time: {:.2f}s'.format(model.eval_time / (epoch + 1)))
            # logging.info('Total embedding time: {:.2f}s'.format(model.emb_time / (epoch + 1)))
            # logging.info('Total classify time: {:.2f}s'.format(model.classify_time / (epoch + 1)))
        logging.info("best_valid_f1: {:.6f}".format(best_valid))
        logging.info("test_f1: {:.6f}".format(test_result))


def seq2seq_att_action_generation(opt, single_turn_history=False):
    use_cuda = opt.gpu >= 0 and torch.cuda.is_available()
    with open(opt.train_path, 'r') as train_file, \
            open(opt.dev_path, 'r') as dev_file, \
            open(opt.test_path, 'r') as test_file, \
            open(opt.dict_path, 'r') as dict_file:
        logging.info('Start loading data from:\ntrain:{}\ndev:{}\ntest:{}\ndict:{}\n'.format(
            opt.train_path, opt.dev_path, opt.test_path, opt.dict_path
        ))
        train_data = json.load(train_file)
        dev_data = json.load(dev_file)
        test_data = json.load(test_file)
        full_dict = json.load(dict_file)

        logging.info('Finish data loading.')
        print('Finish  data loading!!!!!!!!!!')

        # unpack data
        train_input, train_label, train_turn_id = zip(*train_data)
        dev_input, dev_label, dev_turn_id = zip(*dev_data)
        test_input, test_label, test_turn_id = zip(*test_data)

        # stack history
        if not single_turn_history:
            train_input = transform_data_into_history_style(train_input, train_turn_id)
            dev_input = transform_data_into_history_style(dev_input, dev_turn_id)
            test_input = transform_data_into_history_style(test_input, test_turn_id)

        # gen tgt dict
        sos_token = '<SOS>'
        eos_token = '<EOS>'
        pad_token = '<PAD>'
        tgt_token2id_dict = {}
        tgt_id2token_dict = {}
        for token in (
            [pad_token, sos_token, eos_token, 'diaact', 'inform_slots', 'request_slots'] +
            full_dict['user_inform_slot2id'].keys() +
            full_dict['user_request_slot2id'].keys() +
            full_dict['diaact2id'].keys()
        ):
            if token not in tgt_token2id_dict:
                t_id = len(tgt_token2id_dict)
                tgt_token2id_dict[token] = t_id
                tgt_id2token_dict[t_id] = token
        full_dict['tgt_token2id'] = tgt_token2id_dict
        full_dict['tgt_id2token'] = tgt_id2token_dict

        # convert label to sequence
        train_label = transform_label_into_sequence_style(train_label, full_dict, sos_token, eos_token, pad_token)
        dev_label = transform_label_into_sequence_style(dev_label, full_dict, sos_token, eos_token, pad_token)
        test_label = transform_label_into_sequence_style(test_label, full_dict, sos_token, eos_token, pad_token)

        # create batches
        train_x, train_y = create_batches(train_input, train_label, opt.batch_size, use_cuda=use_cuda)
        dev_x, dev_y = create_batches(dev_input, dev_label, opt.batch_size, use_cuda=use_cuda)
        test_x, test_y = create_batches(test_input, test_label, opt.batch_size, use_cuda=use_cuda)
        #
        # for i in range(3):
        #     print('========= DEBUG =========', train_x[0][i])

        input_size = len(train_input[0][0])

        classifier = Seq2SeqActionGenerator(
            input_size=input_size, hidden_size=opt.hidden_dim, n_layers=opt.depth,
            tgt_vocb_size=len(tgt_token2id_dict), max_len=opt.max_len, dropout_p=opt.dropout,
            sos_id=tgt_token2id_dict[sos_token], eos_id=tgt_token2id_dict[eos_token],
            token2id=tgt_token2id_dict, id2token=tgt_id2token_dict, opt=opt,
            bidirectional=opt.direction == 'bi', use_attention=True, input_variable_lengths=False,
            use_cuda=use_cuda
        )

        optimizer = optim.Adam(classifier.parameters(), lr=opt.lr)

        best_valid, test_result = -1e8, -1e8
        for epoch in range(opt.max_epoch):
            best_valid, test_result = train_model(
                epoch=epoch,
                model=classifier,
                optimizer=optimizer,
                train_x=train_x, train_y=train_y,
                valid_x=dev_x, valid_y=dev_y,
                test_x=test_x, test_y=test_y,
                full_dict=full_dict, best_valid=best_valid, test_f1_score=test_result
            )
            if opt.lr_decay > 0:
                optimizer.param_groups[0]['lr'] *= opt.lr_decay  # there is only one group, so use index 0
            # logging.info('Total encoder time: {:.2f}s'.format(model.eval_time / (epoch + 1)))
            # logging.info('Total embedding time: {:.2f}s'.format(model.emb_time / (epoch + 1)))
            # logging.info('Total classify time: {:.2f}s'.format(model.classify_time / (epoch + 1)))
        logging.info("best_valid_f1: {:.6f}".format(best_valid))
        logging.info("test_f1: {:.6f}".format(test_result))


def convert_data_into_state_style(datas, full_dict, sample_style='multi-hot', history_turns=1):
    ret = []
    for item in datas:
        dialog_state_dict = vector2state(item, full_dict)
        # Content:
        # {
        #         'state_vectors': state_vector_components,
        #         'user_goal': user_goal,
        #         'sys_action': sys_action,  # request slots, inform slots, diaact, speaker
        #         'history_slots': history_slots,  # pos: informed, neg: not_informed, irr: irrelevant
        #         'rest_slots': rest_slots,  # pos: remained, neg: already_got, irrelevant
        #         'consistent_slots': consistent_slots,  # pos: consistent, neg: inconsistent, irrelevant
        #         'dialog_status': dialog_status,
        # }
        if sample_style == 'multi-hot':
            sample = dialog_state_dict['state_vector_list']
        else:
            raise NotImplementedError

        if history_turns == 1:
            ret.append(sample)
        elif history_turns == -1:
            ''' no history length limit, do padding here '''
            raise NotImplementedError
        else:
            raise NotImplementedError
    return ret


def state2seq_action_generation(opt):
    use_cuda = opt.gpu >= 0 and torch.cuda.is_available()
    with open(opt.train_path, 'r') as train_file, \
            open(opt.dev_path, 'r') as dev_file, \
            open(opt.test_path, 'r') as test_file, \
            open(opt.dict_path, 'r') as dict_file:
        logging.info('Start loading data from:\ntrain:{}\ndev:{}\ntest:{}\ndict:{}\n'.format(
            opt.train_path, opt.dev_path, opt.test_path, opt.dict_path
        ))
        train_data = json.load(train_file)
        dev_data = json.load(dev_file)
        test_data = json.load(test_file)
        full_dict = json.load(dict_file)

        logging.info('Finish data loading.')
        print('Finish  data loading!!!!!!!!!!')

        ''' unpack data '''
        train_input, train_label, train_turn_id = zip(*train_data)
        dev_input, dev_label, dev_turn_id = zip(*dev_data)
        test_input, test_label, test_turn_id = zip(*test_data)

        ''' change input to a state format of multi-vector'''
        train_input = convert_data_into_state_style(train_input, full_dict)
        dev_input = convert_data_into_state_style(dev_input, full_dict)
        test_input = convert_data_into_state_style(test_input, full_dict)

        ''' gen tgt dict '''
        sos_token = '<SOS>'
        eos_token = '<EOS>'
        pad_token = '<PAD>'
        tgt_token2id_dict = {}
        tgt_id2token_dict = {}
        for token in (
            [pad_token, sos_token, eos_token, 'diaact', 'inform_slots', 'request_slots'] +
            full_dict['user_inform_slot2id'].keys() +
            full_dict['user_request_slot2id'].keys() +
            full_dict['diaact2id'].keys()
        ):
            if token not in tgt_token2id_dict:
                t_id = len(tgt_token2id_dict)
                tgt_token2id_dict[token] = t_id
                tgt_id2token_dict[t_id] = token
        full_dict['tgt_token2id'] = tgt_token2id_dict
        full_dict['tgt_id2token'] = tgt_id2token_dict

        ''' convert label to sequence '''
        train_label = transform_label_into_sequence_style(train_label, full_dict, sos_token, eos_token, pad_token)
        dev_label = transform_label_into_sequence_style(dev_label, full_dict, sos_token, eos_token, pad_token)
        test_label = transform_label_into_sequence_style(test_label, full_dict, sos_token, eos_token, pad_token)

        ''' create batches '''
        # input for each sample is a list, so don't change to tensor here
        train_x, train_y = create_batches(train_input, train_label, opt.batch_size, use_cuda=use_cuda, tensor=False)
        dev_x, dev_y = create_batches(dev_input, dev_label, opt.batch_size, use_cuda=use_cuda, tensor=False)
        test_x, test_y = create_batches(test_input, test_label, opt.batch_size, use_cuda=use_cuda, tensor=False)

        input_size = len(train_input[0][0])

        classifier = State2Seq(
            slot_num=len(full_dict['user_inform_slot2id']), diaact_num=len(full_dict['diaact2id']),
            embedded_v_size=opt.embedded_v_size, state_v_component=HARD_CODED_V_COMPONENT,
            hidden_size=opt.hidden_dim, n_layers=opt.depth,
            tgt_vocb_size=len(tgt_token2id_dict), max_len=opt.max_len, dropout_p=opt.dropout,
            sos_id=tgt_token2id_dict[sos_token], eos_id=tgt_token2id_dict[eos_token],
            token2id=tgt_token2id_dict, id2token=tgt_id2token_dict, opt=opt,
            bidirectional=False, use_attention=True, input_variable_lengths=False,
            use_cuda=use_cuda
        )

        optimizer = optim.Adam(classifier.parameters(), lr=opt.lr)

        best_valid, test_result = -1e8, -1e8
        for epoch in range(opt.max_epoch):
            best_valid, test_result = train_model(
                epoch=epoch,
                model=classifier,
                optimizer=optimizer,
                train_x=train_x, train_y=train_y,
                valid_x=dev_x, valid_y=dev_y,
                test_x=test_x, test_y=test_y,
                full_dict=full_dict, best_valid=best_valid, test_f1_score=test_result
            )
            if opt.lr_decay > 0:
                optimizer.param_groups[0]['lr'] *= opt.lr_decay  # there is only one group, so use index 0
            # logging.info('Total encoder time: {:.2f}s'.format(model.eval_time / (epoch + 1)))
            # logging.info('Total embedding time: {:.2f}s'.format(model.emb_time / (epoch + 1)))
            # logging.info('Total classify time: {:.2f}s'.format(model.classify_time / (epoch + 1)))
        logging.info("best_valid_f1: {:.6f}".format(best_valid))
        logging.info("test_f1: {:.6f}".format(test_result))


def state2seq_no_att_action_generation(opt):
    use_cuda = opt.gpu >= 0 and torch.cuda.is_available()
    with open(opt.train_path, 'r') as train_file, \
            open(opt.dev_path, 'r') as dev_file, \
            open(opt.test_path, 'r') as test_file, \
            open(opt.dict_path, 'r') as dict_file:
        logging.info('Start loading data from:\ntrain:{}\ndev:{}\ntest:{}\ndict:{}\n'.format(
            opt.train_path, opt.dev_path, opt.test_path, opt.dict_path
        ))
        train_data = json.load(train_file)
        dev_data = json.load(dev_file)
        test_data = json.load(test_file)
        full_dict = json.load(dict_file)

        logging.info('Finish data loading.')
        print('Finish  data loading!!!!!!!!!!')

        ''' unpack data '''
        train_input, train_label, train_turn_id = zip(*train_data)
        dev_input, dev_label, dev_turn_id = zip(*dev_data)
        test_input, test_label, test_turn_id = zip(*test_data)

        ''' change input to a state format of multi-vector'''
        train_input = convert_data_into_state_style(train_input, full_dict)
        dev_input = convert_data_into_state_style(dev_input, full_dict)
        test_input = convert_data_into_state_style(test_input, full_dict)

        ''' gen tgt dict '''
        sos_token = '<SOS>'
        eos_token = '<EOS>'
        pad_token = '<PAD>'
        tgt_token2id_dict = {}
        tgt_id2token_dict = {}
        for token in (
            [pad_token, sos_token, eos_token, 'diaact', 'inform_slots', 'request_slots'] +
            full_dict['user_inform_slot2id'].keys() +
            full_dict['user_request_slot2id'].keys() +
            full_dict['diaact2id'].keys()
        ):
            if token not in tgt_token2id_dict:
                t_id = len(tgt_token2id_dict)
                tgt_token2id_dict[token] = t_id
                tgt_id2token_dict[t_id] = token
        full_dict['tgt_token2id'] = tgt_token2id_dict
        full_dict['tgt_id2token'] = tgt_id2token_dict

        ''' convert label to sequence '''
        train_label = transform_label_into_sequence_style(train_label, full_dict, sos_token, eos_token, pad_token)
        dev_label = transform_label_into_sequence_style(dev_label, full_dict, sos_token, eos_token, pad_token)
        test_label = transform_label_into_sequence_style(test_label, full_dict, sos_token, eos_token, pad_token)

        ''' create batches '''
        # input for each sample is a list, so don't change to tensor here
        train_x, train_y = create_batches(train_input, train_label, opt.batch_size, use_cuda=use_cuda, tensor=False)
        dev_x, dev_y = create_batches(dev_input, dev_label, opt.batch_size, use_cuda=use_cuda, tensor=False)
        test_x, test_y = create_batches(test_input, test_label, opt.batch_size, use_cuda=use_cuda, tensor=False)

        input_size = len(train_input[0][0])

        classifier = State2Seq(
            slot_num=len(full_dict['user_inform_slot2id']), diaact_num=len(full_dict['diaact2id']),
            embedded_v_size=opt.embedded_v_size, state_v_component=HARD_CODED_V_COMPONENT,
            hidden_size=opt.hidden_dim, n_layers=opt.depth,
            tgt_vocb_size=len(tgt_token2id_dict), max_len=opt.max_len, dropout_p=opt.dropout,
            sos_id=tgt_token2id_dict[sos_token], eos_id=tgt_token2id_dict[eos_token],
            token2id=tgt_token2id_dict, id2token=tgt_id2token_dict, opt=opt,
            bidirectional=False, use_attention=False, input_variable_lengths=False,
            use_cuda=use_cuda
        )

        optimizer = optim.Adam(classifier.parameters(), lr=opt.lr)

        best_valid, test_result = -1e8, -1e8
        for epoch in range(opt.max_epoch):
            best_valid, test_result = train_model(
                epoch=epoch,
                model=classifier,
                optimizer=optimizer,
                train_x=train_x, train_y=train_y,
                valid_x=dev_x, valid_y=dev_y,
                test_x=test_x, test_y=test_y,
                full_dict=full_dict, best_valid=best_valid, test_f1_score=test_result
            )
            if opt.lr_decay > 0:
                optimizer.param_groups[0]['lr'] *= opt.lr_decay  # there is only one group, so use index 0
            # logging.info('Total encoder time: {:.2f}s'.format(model.eval_time / (epoch + 1)))
            # logging.info('Total embedding time: {:.2f}s'.format(model.emb_time / (epoch + 1)))
            # logging.info('Total classify time: {:.2f}s'.format(model.classify_time / (epoch + 1)))
        logging.info("best_valid_f1: {:.6f}".format(best_valid))
        logging.info("test_f1: {:.6f}".format(test_result))


if __name__ == '__main__':
    main()
