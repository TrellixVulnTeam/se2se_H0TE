# Copyright 2015 The TensorFlow Authors. All Rights Reserved.
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

"""Binary for training translation models and decoding from them.

Running this program without --decode will download the WMT corpus into
the directory specified as --data_dir and tokenize it in a very basic way,
and then start training a model saving checkpoints to --train_dir.

Running with --decode starts an interactive loop so you can see how
the current checkpoint translates English sentences into French.

See the following papers for more information on neural translation models.
 * http://arxiv.org/abs/1409.3215
 * http://arxiv.org/abs/1409.0473
 * http://arxiv.org/abs/1412.2007
"""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import math
import os
import random
import sys
import time

import numpy as np
from six.moves import xrange  # pylint: disable=redefined-builtin
import tensorflow as tf

import data_utils
import seq2seq_model

import numberExp as ne

tf.app.flags.DEFINE_float("_learning_rate", 0.5, "Learning rate.")
tf.app.flags.DEFINE_float("learning_rate_decay_factor", 0.99,
                          "Learning rate decays by this much.")
tf.app.flags.DEFINE_float("max_gradient_norm", 1.0,
                          "Clip gradients to this norm.")
tf.app.flags.DEFINE_integer("batch_size", 80,
                            "Batch size to use during training.")
tf.app.flags.DEFINE_integer("size", 1000, "Size of each model layer.")
tf.app.flags.DEFINE_integer("num_layers", 1, "Number of layers in the model.")
tf.app.flags.DEFINE_integer("en_vocab_size", 30000, "English vocabulary size.")
tf.app.flags.DEFINE_integer("fr_vocab_size", 30000, "French vocabulary size.")
tf.app.flags.DEFINE_string("data_dir", "./data/", "Data directory")
tf.app.flags.DEFINE_string("train_dir", "./train/", "Training directory.")
tf.app.flags.DEFINE_integer("max_train_data_size", 0,
                            "Limit on the size of training data (0: no limit).")
tf.app.flags.DEFINE_integer("steps_per_checkpoint", 1000,
                            "How many training steps to do per checkpoint.")
tf.app.flags.DEFINE_boolean("decode", False,
                            "Set to True for interactive decoding.")
tf.app.flags.DEFINE_boolean("self_test", True,
                            "Run a self-test if this is set to True.")
tf.app.flags.DEFINE_boolean("use_fp16", False,
                            "Train using fp16 instead of fp32.")
tf.app.flags.DEFINE_integer("beam_size", 5,
                            "The size of beam search. Do greedy search when set this to 1.")
tf.app.flags.DEFINE_string("model", None, "the checkpoint model to load")

FLAGS = tf.app.flags.FLAGS

# We use a number of buckets and pad to the closest one for efficiency.
# See seq2seq_model.Seq2SeqModel for details of how they work.
_buckets = [(10, 10), (15, 15), (25, 25), (40, 40)]


def read_data(source_path, target_path, max_size=None):
    """Read data from source and target files and put into buckets.

    Args:
      source_path: path to the files with token-ids for the source language.
      target_path: path to the file with token-ids for the target language;
        it must be aligned with the source file: n-th line contains the desired
        output for n-th line from the source_path.
      max_size: maximum number of lines to read, all other will be ignored;
        if 0 or None, data files will be read completely (no limit).

    Returns:
      data_set: a list of length len(_buckets); data_set[n] contains a list of
        (source, target) pairs read from the provided data files that fit
        into the n-th bucket, i.e., such that len(source) < _buckets[n][0] and
        len(target) < _buckets[n][1]; source and target are lists of token-ids.
    """
    data_set = [[] for _ in _buckets]
    with tf.gfile.GFile(source_path, mode="r") as source_file:
        with tf.gfile.GFile(target_path, mode="r") as target_file:
            source, target = source_file.readline(), target_file.readline()
            counter = 0
            while source and target and (not max_size or counter < max_size):
                counter += 1
                if counter % 100000 == 0:
                    print("  reading data line %d" % counter)
                    sys.stdout.flush()
                source_ids = [int(x) for x in source.split()]
                target_ids = [int(x) for x in target.split()]
                target_ids.append(data_utils.EOS_ID)
                for bucket_id, (source_size, target_size) in enumerate(_buckets):
                    if len(source_ids) < source_size and len(target_ids) < target_size:
                        data_set[bucket_id].append([source_ids, target_ids])
                        break
                source, target = source_file.readline(), target_file.readline()

    return data_set


def create_model(session, forward_only, ckpt_file=None):
    """Create translation model and initialize or load parameters in session."""
    dtype = tf.float16 if FLAGS.use_fp16 else tf.float32
    model = seq2seq_model.Seq2SeqModel(
            FLAGS.en_vocab_size,
            FLAGS.fr_vocab_size,
            _buckets,
            FLAGS.size,
            FLAGS.num_layers,
            FLAGS.max_gradient_norm,
            FLAGS.batch_size,
            FLAGS._learning_rate,
            FLAGS.learning_rate_decay_factor,
            FLAGS.beam_size,
            forward_only=forward_only,
            dtype=dtype)
    if ckpt_file:
        model_path = os.path.join(FLAGS.train_dir, ckpt_file)
        if tf.gfile.Exists(model_path):
            sys.stderr.write("Reading model parameters from %s\n" % model_path)
            sys.stderr.flush()
            model.saver.restore(session, model_path)
    else:
        ckpt = tf.train.get_checkpoint_state(FLAGS.train_dir)
        if ckpt and tf.gfile.Exists(ckpt.model_checkpoint_path):
            print("Reading model parameters from %s" % ckpt.model_checkpoint_path)
            model.saver.restore(session, ckpt.model_checkpoint_path)
        else:
            print("Created model with fresh parameters.")
            session.run(tf.initialize_all_variables())
    return model


def train():
    """Train a en->fr translation model using WMT data."""
    # Prepare WMT data.
    print("Preparing WMT data in %s" % FLAGS.data_dir)
    en_train, fr_train, en_dev, fr_dev, en_vocab_path, fr_vocab_path = data_utils.prepare_wmt_data(
            FLAGS.data_dir, FLAGS.en_vocab_size, FLAGS.fr_vocab_size)

    with tf.Session() as sess:
        # Create model.
        print("Creating %d layers of %d units." % (FLAGS.num_layers, FLAGS.size))
        model = create_model(sess, False)

        # Read data into buckets and compute their sizes.
        print("Reading development and training data (limit: %d)."
              % FLAGS.max_train_data_size)
        dev_set = read_data(en_dev, fr_dev)
        # print("here finished dev")
        train_set = read_data(en_train, fr_train, FLAGS.max_train_data_size)
        # print("finished train")
        train_bucket_sizes = [len(train_set[b]) for b in xrange(len(_buckets))]
        train_total_size = float(sum(train_bucket_sizes))

        # A bucket scale is a list of increasing numbers from 0 to 1 that we'll use
        # to select a bucket. Length of [scale[i], scale[i+1]] is proportional to
        # the size if i-th training bucket, as used later.
        train_buckets_scale = [sum(train_bucket_sizes[:i + 1]) / train_total_size
                               for i in xrange(len(train_bucket_sizes))]

        # This is the training loop.
        step_time, loss = 0.0, 0.0
        current_step = 0
        previous_losses = []
        while True:
            # Choose a bucket according to data distribution. We pick a random number
            # in [0, 1] and use the corresponding interval in train_buckets_scale.
            random_number_01 = np.random.random_sample()
            bucket_id = min([i for i in xrange(len(train_buckets_scale))
                             if train_buckets_scale[i] > random_number_01])

            # Get a batch and make a step.
            start_time = time.time()
            encoder_inputs, decoder_inputs, target_weights = model.get_batch(
                    train_set, bucket_id)
            _, step_loss, _ = model.step(sess, encoder_inputs, decoder_inputs,
                                         target_weights, bucket_id, False)
            step_time += (time.time() - start_time) / FLAGS.steps_per_checkpoint
            loss += step_loss / FLAGS.steps_per_checkpoint
            current_step += 1

            # Once in a while, we save checkpoint, print statistics, and run evals.
            if current_step % FLAGS.steps_per_checkpoint == 0:
                # Print statistics for the previous epoch.
                perplexity = math.exp(float(loss)) if loss < 300 else float("inf")
                print("global step %d learning rate %.4f step-time %.2f perplexity "
                      "%.2f" % (model.global_step.eval(), model.learning_rate.eval(),
                                step_time, perplexity))
                # print("my time %.2f" % time.time())
                # Decrease learning rate if no improvement was seen over last 3 times.
                if len(previous_losses) > 2 and loss > max(previous_losses[-3:]):
                    sess.run(model.learning_rate_decay_op)
                previous_losses.append(loss)
                # Save checkpoint and zero timer and loss.
                checkpoint_path = os.path.join(FLAGS.train_dir, "translate.ckpt")
                model.saver.save(sess, checkpoint_path, global_step=model.global_step)
                step_time, loss = 0.0, 0.0
                # Run evals on development set and print their perplexity.
                for bucket_id in xrange(len(_buckets)):
                    if len(dev_set[bucket_id]) == 0:
                        print("  eval: empty bucket %d" % (bucket_id))
                        continue
                    encoder_inputs, decoder_inputs, target_weights = model.get_batch(
                            dev_set, bucket_id)
                    _, eval_loss, _ = model.step(sess, encoder_inputs, decoder_inputs,
                                                 target_weights, bucket_id, True)
                    eval_ppx = math.exp(float(eval_loss)) if eval_loss < 300 else float(
                            "inf")
                    print("  eval: bucket %d perplexity %.2f" % (bucket_id, eval_ppx))
                sys.stdout.flush()


def decode():
    with tf.Session() as sess:
        # Create model and load parameters.
        model = create_model(sess, True, FLAGS.model)
        model.batch_size = 1  # We decode one sentence at a time.

        # Load vocabularies.
        en_vocab_path = os.path.join(FLAGS.data_dir,
                                     "vocab%d.en" % FLAGS.en_vocab_size)
        fr_vocab_path = os.path.join(FLAGS.data_dir,
                                     "vocab%d.fr" % FLAGS.fr_vocab_size)
        en_vocab, _ = data_utils.initialize_vocabulary(en_vocab_path)
        _, rev_fr_vocab = data_utils.initialize_vocabulary(fr_vocab_path)

        # Decode from standard input.
        # sys.stdout.write("> ")
        sys.stdout.flush()
        sentence = sys.stdin.readline()
        while sentence:
            # Get token-ids for the input sentence.
            token_ids = data_utils.sentence_to_token_ids(tf.compat.as_bytes(sentence), en_vocab)
            # Which bucket does it belong to?
            bucket_id = [b for b in xrange(len(_buckets))
                         if _buckets[b][0] > len(token_ids)]
            if bucket_id:
                bucket_id = min(bucket_id)
            else:
                bucket_id = len(_buckets) - 1
            # Get a 1-element batch to feed the sentence to the model.
            encoder_inputs, decoder_inputs, target_weights = model.get_batch(
                    {bucket_id: [(token_ids, [])]}, bucket_id)
            # Get output logits for the sentence.
            _, _, output_logits = model.step(sess, encoder_inputs, decoder_inputs,
                                             target_weights, bucket_id, True)
            # This is a greedy decoder - outputs are just argmaxes of output_logits.
            # outputs = [int(np.argmax(logit, axis=1)) for logit in output_logits]
            # beam search best result
            outputs = [int(logit) for logit in output_logits]
            # If there is an EOS symbol in outputs, cut them at that point.
            if data_utils.EOS_ID in outputs:
                outputs = outputs[:outputs.index(data_utils.EOS_ID)]
            # Print out French sentence corresponding to outputs.
            print(" ".join([tf.compat.as_str(rev_fr_vocab[output]) for output in outputs]))
            # print("> ", end="")
            sys.stdout.flush()
            sentence = sys.stdin.readline()


def self_test():
    """Test the translation model."""
    with tf.Session() as sess:
        
        print("Self-test for neural translation model.")
        # Create model with vocabulariecds of 10, 2 small buckets, 2 layers of 32.
        model = seq2seq_model.Seq2SeqModel(20, 20, [(13, 13), (13, 13)], 128, 2,
                                           5.0, 32, 0.3, 0.99,5, num_samples=8)
        sess.run(tf.initialize_all_variables())
        
        data_set=[]
        for m in xrange(10000):
            data_set=my_data()
            for _ in xrange(10):  # Train the fake model for 5 steps.
                bucket_id = random.choice([0, 1])
                encoder_inputs, decoder_inputs, target_weights = model.get_batch(
                        data_set, bucket_id)
                '''
                for s in range(len(encoder_inputs)):
                    print(encoder_inputs[s])
                print('+++++++++++++++++++++++++++++++++++++++++++++')
                for s in range(len(decoder_inputs)):
                    print(decoder_inputs[s])
                '''
                
                _a,_b,_c=model.step(sess, encoder_inputs, decoder_inputs, target_weights,
                                    bucket_id, False)
                #print(_b)
                
        saver = tf.train.Saver()
        saver.save(sess, "train/translate.ckpt")
        #print('+++++++++++++++++++++++++++++++++++++++++++++')
        saver.restore(sess, "train/translate.ckpt")
        for j in range(1):
            data_set=my_data()
            bucket_id = random.choice([0, 1])
            encoder_inputs, decoder_inputs, target_weights = model.get_batch(data_set, bucket_id)
            _a,_b,_c=model.step(sess, encoder_inputs, decoder_inputs, target_weights,bucket_id, False)
            #print(_b)
         
        #print('+++++++++++++++++++++++++++++++++++++++++++++')
        res_logits=[]
        einputs=[]
        dinputs=[]
        for j in range(10110):
            data_set=my_data()
            #print('----------------------------------------')
            bucket_id = random.choice([0, 1])
            encoder_inputs, decoder_inputs, target_weights = model.get_batch(data_set, bucket_id)
            _a, _b, output_logits=model.step(sess, encoder_inputs, decoder_inputs, target_weights,bucket_id, True)
            
            #print(len(output_logits))
            #print(len(output_logits[0]))
            #print(len(output_logits[0][0]))
            #print(_b)
            
            
            einputs.append(encoder_inputs)
            dinputs.append(decoder_inputs)
            res_logits.append(output_logits)
        return einputs,dinputs,res_logits#(13,32,128)
        
def self_decode():
    """Test the translation model."""
    with tf.Session() as sess:
        
        print("Self-test for neural translation model.")
        # Create model with vocabulariecds of 10, 2 small buckets, 2 layers of 32.
        model = seq2seq_model.Seq2SeqModel(20, 20, [(13, 13), (13, 13)], 128, 2,
                                           5.0, 32, 0.3, 0.99,5, num_samples=8)
        sess.run(tf.initialize_all_variables())
        
        saver = tf.train.Saver()
        saver.restore(sess, "train/translate.ckpt")
      
        res_logits=[]
        einputs=[]
        dinputs=[]
        for j in range(1):
            data_set=my_data()
            #print('----------------------------------------')
            bucket_id = random.choice([0, 1])
            encoder_inputs, decoder_inputs, target_weights = model.get_batch(data_set, bucket_id)
            _a, _b, output_logits=model.step(sess, encoder_inputs, decoder_inputs, target_weights,bucket_id, True)
            
            #print(len(output_logits))
            #print(len(output_logits[0]))
            #print(len(output_logits[0][0]))
            #print(_b)
            
            
            einputs.append(encoder_inputs)
            dinputs.append(decoder_inputs)
            res_logits.append(output_logits)
        return einputs,dinputs,res_logits#(13,32,128)           

def my_data():
    x_n,y_n=ne.createExp(10,6)
    #print(x_n)
    #print(y_n)
    
    data_set=([([],[]),([],[]),([],[])],[([],[]),([],[]),([],[])])
    for i in range(3):
        xi=x_n[i]
        yi=y_n[i]
        for j in range(len(xi)):
            data_set[0][i][0].append(int(xi[j]))
        for j in range(len(yi)):
            data_set[0][i][1].append(int(yi[j]))
        
    for i in range(3):
        xi=x_n[i+3]
        yi=y_n[i+3]
        for j in range(len(xi)):
            data_set[1][i][0].append(int(xi[j]))
        for j in range(len(yi)):
            data_set[1][i][1].append(int(yi[j]))
    #print(data_set)
    return data_set

def self_test1():
    with tf.Session() as sess:
        model = seq2seq_model.Seq2SeqModel(20, 20, [(13, 13), (13, 13)], 128, 2,5.0, 32, 0.3, 0.99,1, num_samples=8,forward_only=True)
        print('xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx')
        sess.run(tf.initialize_all_variables())
        
        data_set=my_data()
        bucket_id = random.choice([0, 1])
        encoder_inputs, decoder_inputs, target_weights = model.get_batch(data_set, bucket_id)
        _a,_b,_c=model.step(sess, encoder_inputs, decoder_inputs, target_weights,
                                    bucket_id, True)
                                    
        print(len(_c))
        print(len(_c[0]))
        print(len(_c[0][0]))
        print(len(_c[0][0][0]))

def main(_):
    if FLAGS.self_test:
        self_test()
    elif FLAGS.decode:
        decode()
    else:
        train()


if __name__ == "__main__":
    tf.app.run()
