#!/usr/bin/python3
"""
Script used for training on argus anssel dataset.
Prerequisites:
    * Get glove.6B.50d.txt from http://nlp.stanford.edu/projects/glove/
"""
from __future__ import print_function
from __future__ import division

import importlib
import sys
import csv
import pickle

from keras.callbacks import ModelCheckpoint
from keras.layers.core import Activation
from keras.models import Graph

import pysts.embedding as emb
import pysts.eval as ev
import pysts.loader as loader
import pysts.nlp as nlp
from pysts.hyperparam import hash_params
from pysts.vocab import Vocabulary

from pysts.kerasts import graph_input_anssel
from pysts.kerasts.callbacks import AnsSelBinCB
import pysts.kerasts.blocks as B
from keras.layers.embeddings import Embedding
from keras.layers.core import Dropout
import numpy as np
import keras.preprocessing.sequence as prep
from layer import Reshape_, ClasRel

s0pad = 60
s1pad = 60


# Used when tokenizing words
sentence_re = r'''(?x)      # set flag to allow verbose regexps
      ([A-Z])(\.[A-Z])+\.?  # abbreviations, e.g. U.S.A.
    | \w+(-\w+)*            # words with optional internal hyphens
    | \$?\d+(\.\d+)?%?      # currency and percentages, e.g. $12.40, 82%
    | \.\.\.                # ellipsis
    | [][.,;"'?():-_`]      # these are separate tokens
'''

import nltk
def tokenize(string):
    return nltk.regexp_tokenize(string, sentence_re)


class Q:
    def __init__(self, qtext, q, s, c, r, y):
        self.qtext = qtext  # str of question
        self.q = q  # list of one repeated tokenized question
        self.s = s  # list of tokenized sentences
        self.c = c  # class features, shape = (nb_sentences, w_dim)
        self.r = r  # rel features, shape = (nb_sentences, q_dim)
        self.y = y  # GS scalar


def load_sets(qs, max_sentences, vocab=None):
    # s0=questions, s1=sentences
    if vocab is None:
        s0, s1 = [], []
        for q in qs:
            s0 += q.q
            s1 += q.s
        vocab = Vocabulary(s0 + s1)
    si03d, si13d, f04d, f14d = [], [], [], []
    for q in qs:
        s0 = q.q
        s1 = q.s
        si0 = vocab.vectorize(s0, spad=s0pad)
        si1 = vocab.vectorize(s1, spad=s1pad)
        si0 = prep.pad_sequences(si0.T, maxlen=max_sentences, padding='post', truncating='post').T
        si1 = prep.pad_sequences(si1.T, maxlen=max_sentences, padding='post', truncating='post').T
        si03d.append(si0)
        si13d.append(si1)

        f0, f1 = nlp.sentence_flags(s0, s1, s0pad, s1pad)
        f0 = prep.pad_sequences(f0.transpose((1, 0, 2)), maxlen=max_sentences,
                                padding='post', truncating='post', dtype='bool').transpose((1, 0, 2))
        f1 = prep.pad_sequences(f1.transpose((1, 0, 2)), maxlen=max_sentences,
                                padding='post', truncating='post', dtype='bool').transpose((1, 0, 2))
        f04d.append(f0)
        f14d.append(f1)

    # ==========================================
    c = np.array([prep.pad_sequences(q.c.T, maxlen=max_sentences, padding='post',
                                     truncating='post', dtype='float32') for q in qs])
    r = np.array([prep.pad_sequences(q.r.T, maxlen=max_sentences, padding='post',
                                     truncating='post', dtype='float32') for q in qs])
    x = np.concatenate((c, r), axis=1)
    clr = x.transpose((0, 2, 1))
    y = np.array([q.y for q in qs])

    gr = {'si03d': np.array(si03d), 'si13d': np.array(si13d),
          'clr_in': clr, 'score': y}
    if f0 is not None:
        gr['f04d'] = np.array(f04d)
        gr['f14d'] = np.array(f14d)

    return y, vocab, gr


def config(module_config, params, epochs=2):
    c = dict()
    c['embdim'] = 50
    c['inp_e_dropout'] = 0.
    c['e_add_flags'] = True

    c['ptscorer'] = B.mlp_ptscorer
    c['mlpsum'] = 'sum'
    c['Ddim'] = 1

    c['loss'] = 'binary_crossentropy'
    c['nb_epoch'] = epochs

    c['class_mode'] = 'binary'
    module_config(c)

    for p in params:
        k, v = p.split('=')
        c[k] = eval(v)

    ps, h = hash_params(c)
    return c, ps, h


def prep_model(model, glove, vocab, module_prep_model, c, oact, s0pad, s1pad):
    # Input embedding and encoding
    N = embedding(model, glove, vocab, s0pad, s1pad, c['inp_e_dropout'], add_flags=c['e_add_flags'])

    # Sentence-aggregate embeddings
    final_outputs = module_prep_model(model, N, s0pad, s1pad, c)

    # Measurement

    if c['ptscorer'] == '1':
        # special scoring mode just based on the answer
        # (assuming that the question match is carried over to the answer
        # via attention or another mechanism)
        ptscorer = B.cat_ptscorer
        final_outputs = final_outputs[1]
    else:
        ptscorer = c['ptscorer']

    kwargs = dict()
    if ptscorer == B.mlp_ptscorer:
        kwargs['sum_mode'] = c['mlpsum']
    model.add_node(name='scoreS', input=ptscorer(model, final_outputs, c['Ddim'], N, c['l2reg'], **kwargs),
                   layer=Activation(oact))


def build_model(model, glove, vocab, module_prep_model, c, s0pad=s0pad, s1pad=s1pad):
    if c['loss'] == 'binary_crossentropy':
        oact = 'sigmoid'
    else:
        # ranking losses require wide output domain
        oact = 'linear'

    prep_model(model, glove, vocab, module_prep_model, c, oact, s0pad, s1pad)


def train_and_eval(runid, module_prep_model, c, glove, vocab, gr, grt,
                   max_sentences, w_dim, q_dim, optimizer='sgd'):
    clr = ClasRel(w_dim=w_dim+1, q_dim=q_dim, init='normal',
                  max_sentences=max_sentences,
                  activation_w='sigmoid', activation_q='sigmoid')

    print('Model')
    model = Graph()

    # ===================== inputs of size (batch_size, max_sentences, s_pad)
    model.add_input('si03d', (max_sentences, s0pad), dtype=int)  # XXX: cannot be cast to int->problem?
    model.add_input('si13d', (max_sentences, s1pad), dtype=int)
    if True:  # if flags
        model.add_input('f04d', (max_sentences, s0pad, nlp.flagsdim))
        model.add_input('f14d', (max_sentences, s1pad, nlp.flagsdim))
        model.add_node(Reshape_((s0pad, nlp.flagsdim)), 'f0', input='f04d')
        model.add_node(Reshape_((s1pad, nlp.flagsdim)), 'f1', input='f14d')

    # ===================== reshape to (batch_size * max_sentences, s_pad)
    model.add_node(Reshape_((s0pad,)), 'si0', input='si03d')
    model.add_node(Reshape_((s1pad,)), 'si1', input='si13d')

    # ===================== connect to sts model
    build_model(model, glove, vocab, module_prep_model, c)  # model with no output
    # ===================== reshape (batch_size * max_sentences,) -> (batch_size, max_sentences, 1)
    model.add_node(Reshape_((max_sentences, 1)), 'sts_in', input='scoreS')

    # ===================== connect sts output to clr input
    model.add_input('clr_in', (max_sentences, w_dim+q_dim))
    model.add_node(Activation('linear'), 'sts_clr', inputs=['sts_in', 'clr_in'],
                   merge_mode='concat', concat_axis=-1)

    # ===================== connect to clr
    model.add_node(layer=clr, name='clr', input='sts_clr')  # clr w_dim+=1
    model.add_output(name='score', input='clr')

    model.compile(optimizer=optimizer, loss={'score': 'binary_crossentropy'})

    load_weights(model, 'sources/models/keras_model.h5')
    print('Training')
    # model.fit(gr, validation_data=grt,
    #           callbacks=[ModelCheckpoint('weights-'+runid+'-bestval.h5',
    #                                      save_best_only=True, monitor='acc', mode='max')],
    #           batch_size=10, nb_epoch=c['nb_epoch'], show_accuracy=True)
    # model.save_weights('weights-'+runid+'-final.h5', overwrite=True)

    print('Predict&Eval (best epoch)')
    model.load_weights('weights-'+runid+'-bestval.h5')
    loss, acc = model.evaluate(gr, show_accuracy=True)
    print('Train: loss=', loss, 'acc=', acc)
    loss, acc = model.evaluate(grt, show_accuracy=True)
    print('Train: loss=', loss, 'acc=', acc)


def embedding(model, glove, vocab, s0pad, s1pad, dropout, trainable=True,
              add_flags=False):
    """ Sts embedding layer, without creating inputs. """

    if add_flags:
        outputs = ['e0[0]', 'e1[0]']
    else:
        outputs = ['e0', 'e1']

    model.add_shared_node(name='emb', inputs=['si0', 'si1'], outputs=outputs,
                          layer=Embedding(input_dim=vocab.size(), input_length=s1pad,
                                          output_dim=glove.N, mask_zero=True,
                                          weights=[vocab.embmatrix(glove)], trainable=trainable))
    if add_flags:
        for m in [0, 1]:
            model.add_node(name='e%d'%(m,), inputs=['e%d[0]'%(m,), 'f%d'%(m,)], merge_mode='concat', layer=Activation('linear'))
        N = glove.N + nlp.flagsdim
    else:
        N = glove.N

    model.add_shared_node(name='embdrop', inputs=['e0', 'e1'], outputs=['e0_', 'e1_'],
                          layer=Dropout(dropout, input_shape=(N,)))

    return N

