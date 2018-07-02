import paddle.fluid as fluid
from paddle.fluid.layers.learning_rate_scheduler import _decay_step_counter
from paddle.fluid.initializer import init_on_cpu
import math


def conv_bn_pool(input,
                 group,
                 out_ch,
                 act="relu",
                 param=None,
                 bias=None,
                 param_0=None,
                 is_test=False,
                 pooling=True):
    tmp = input
    for i in xrange(group):
        tmp = fluid.layers.conv2d(
            input=tmp,
            num_filters=out_ch[i],
            filter_size=3,
            padding=1,
            param_attr=param if param_0 is None else param_0,
            act=None,  # LinearActivation
            use_cudnn=True)
        tmp = fluid.layers.batch_norm(
            input=tmp,
            act=act,
            param_attr=param,
            bias_attr=bias,
            is_test=is_test)
    if pooling:
        tmp = fluid.layers.pool2d(
            input=tmp,
            pool_size=2,
            pool_type='max',
            pool_stride=2,
            use_cudnn=True,
            ceil_mode=True)

    return tmp


def ocr_convs(input, regularizer=None, gradient_clip=None, is_test=False):
    b = fluid.ParamAttr(
        regularizer=regularizer,
        gradient_clip=gradient_clip,
        initializer=fluid.initializer.Normal(0.0, 0.0))
    w0 = fluid.ParamAttr(
        regularizer=regularizer,
        gradient_clip=gradient_clip,
        initializer=fluid.initializer.Normal(0.0, 0.0005))
    w1 = fluid.ParamAttr(
        regularizer=regularizer,
        gradient_clip=gradient_clip,
        initializer=fluid.initializer.Normal(0.0, 0.01))
    tmp = input
    tmp = conv_bn_pool(
        tmp, 2, [16, 16], param=w1, bias=b, param_0=w0, is_test=is_test)

    tmp = conv_bn_pool(tmp, 2, [32, 32], param=w1, bias=b, is_test=is_test)
    tmp = conv_bn_pool(tmp, 2, [64, 64], param=w1, bias=b, is_test=is_test)
    tmp = conv_bn_pool(
        tmp, 2, [128, 128], param=w1, bias=b, is_test=is_test, pooling=False)
    return tmp


def encoder_net(images,
                num_classes,
                rnn_hidden_size=200,
                regularizer=None,
                gradient_clip=None,
                is_test=False):
    conv_features = ocr_convs(
        images,
        regularizer=regularizer,
        gradient_clip=gradient_clip,
        is_test=is_test)
    sliced_feature = fluid.layers.im2sequence(
        input=conv_features,
        stride=[1, 1],
        filter_size=[conv_features.shape[2], 1])

    para_attr = fluid.ParamAttr(
        regularizer=regularizer,
        gradient_clip=gradient_clip,
        initializer=fluid.initializer.Normal(0.0, 0.02))
    bias_attr = fluid.ParamAttr(
        regularizer=regularizer,
        gradient_clip=gradient_clip,
        initializer=fluid.initializer.Normal(0.0, 0.02),
        learning_rate=2.0)
    bias_attr_nobias = fluid.ParamAttr(
        regularizer=regularizer,
        gradient_clip=gradient_clip,
        initializer=fluid.initializer.Normal(0.0, 0.02))

    fc_1 = fluid.layers.fc(input=sliced_feature,
                           size=rnn_hidden_size * 3,
                           param_attr=para_attr,
                           bias_attr=bias_attr_nobias)
    fc_2 = fluid.layers.fc(input=sliced_feature,
                           size=rnn_hidden_size * 3,
                           param_attr=para_attr,
                           bias_attr=bias_attr_nobias)

    gru_forward = fluid.layers.dynamic_gru(
        input=fc_1,
        size=rnn_hidden_size,
        param_attr=para_attr,
        bias_attr=bias_attr,
        candidate_activation='relu')
    gru_backward = fluid.layers.dynamic_gru(
        input=fc_2,
        size=rnn_hidden_size,
        is_reverse=True,
        param_attr=para_attr,
        bias_attr=bias_attr,
        candidate_activation='relu')

    w_attr = fluid.ParamAttr(
        regularizer=regularizer,
        gradient_clip=gradient_clip,
        initializer=fluid.initializer.Normal(0.0, 0.02))
    b_attr = fluid.ParamAttr(
        regularizer=regularizer,
        gradient_clip=gradient_clip,
        initializer=fluid.initializer.Normal(0.0, 0.0))

    fc_out = fluid.layers.fc(input=[gru_forward, gru_backward],
                             size=num_classes + 1,
                             param_attr=w_attr,
                             bias_attr=b_attr)

    return fc_out


def ctc_train_net(images, label, args, num_classes):
    L2_RATE = 0.0004
    LR = 1.0e-3
    MOMENTUM = 0.9
    learning_rate_decay = None
    regularizer = fluid.regularizer.L2Decay(L2_RATE)

    fc_out = encoder_net(images, num_classes, regularizer=regularizer)
    cost = fluid.layers.warpctc(
        input=fc_out, label=label, blank=num_classes, norm_by_times=True)
    sum_cost = fluid.layers.reduce_sum(cost)
    decoded_out = fluid.layers.ctc_greedy_decoder(
        input=fc_out, blank=num_classes)
    casted_label = fluid.layers.cast(x=label, dtype='int64')
    error_evaluator = fluid.evaluator.EditDistance(
        input=decoded_out, label=casted_label)
    inference_program = fluid.default_main_program().clone(for_test=True)
    if learning_rate_decay == "piecewise_decay":
        learning_rate = fluid.layers.piecewise_decay([
            args.total_step / 4, args.total_step / 2, args.total_step * 3 / 4
        ], [LR, LR * 0.1, LR * 0.01, LR * 0.001])
    else:
        learning_rate = LR

    optimizer = fluid.optimizer.Momentum(
        learning_rate=learning_rate, momentum=MOMENTUM)
    _, params_grads = optimizer.minimize(sum_cost)
    model_average = None
    if args.average_window > 0:
        model_average = fluid.optimizer.ModelAverage(
            args.average_window,
            params_grads,
            min_average_window=args.min_average_window,
            max_average_window=args.max_average_window)
    return sum_cost, error_evaluator, inference_program, model_average


def ctc_infer(images, num_classes):
    fc_out = encoder_net(images, num_classes, is_test=True)
    return fluid.layers.ctc_greedy_decoder(input=fc_out, blank=num_classes)


def ctc_eval(images, label, num_classes):
    fc_out = encoder_net(images, num_classes, is_test=True)
    decoded_out = fluid.layers.ctc_greedy_decoder(
        input=fc_out, blank=num_classes)

    casted_label = fluid.layers.cast(x=label, dtype='int64')
    error_evaluator = fluid.evaluator.EditDistance(
        input=decoded_out, label=casted_label)

    cost = fluid.layers.warpctc(
        input=fc_out, label=label, blank=num_classes, norm_by_times=True)

    return error_evaluator, cost