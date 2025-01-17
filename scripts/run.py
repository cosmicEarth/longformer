import logging
import os
import math
import copy
import torch
from dataclasses import dataclass, field
from transformers import RobertaForMaskedLM, RobertaTokenizerFast, TextDataset, DataCollatorForLanguageModeling, Trainer
from transformers import TrainingArguments, HfArgumentParser
from transformers.modeling_longformer import LongformerSelfAttention
import transformers

import re
import wandb

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

transformers.logging.set_verbosity_info()


class RobertaLongSelfAttention(LongformerSelfAttention):
    def forward(
        self,
        hidden_states,
        attention_mask=None,
        head_mask=None,
        encoder_hidden_states=None,
        encoder_attention_mask=None,
        output_attentions=False,
    ):
        return super().forward(hidden_states, attention_mask=attention_mask, output_attentions=output_attentions)


class RobertaLongForMaskedLM(RobertaForMaskedLM):
    def __init__(self, config):
        super().__init__(config)
        for i, layer in enumerate(self.roberta.encoder.layer):
            # replace the `modeling_bert.BertSelfAttention` object with `LongformerSelfAttention`
            layer.attention.self = RobertaLongSelfAttention(config, layer_id=i)


def create_long_model(save_model_to, attention_window, max_pos):
    model = RobertaForMaskedLM.from_pretrained('roberta-base')
    tokenizer = RobertaTokenizerFast.from_pretrained(
        'roberta-base', model_max_length=max_pos)
    config = model.config

    # extend position embeddings
    tokenizer.model_max_length = max_pos
    tokenizer.init_kwargs['model_max_length'] = max_pos
    current_max_pos, embed_size = model.roberta.embeddings.position_embeddings.weight.shape
    max_pos += 2  # NOTE: RoBERTa has positions 0,1 reserved, so embedding size is max position + 2
    config.max_position_embeddings = max_pos
    assert max_pos > current_max_pos
    # allocate a larger position embedding matrix
    new_pos_embed = model.roberta.embeddings.position_embeddings.weight.new_empty(
        max_pos, embed_size)
    # copy position embeddings over and over to initialize the new position embeddings
    k = 2
    step = current_max_pos - 2
    while k < max_pos - 1:
        new_pos_embed[k:(
            k + step)] = model.roberta.embeddings.position_embeddings.weight[2:]
        k += step
    model.roberta.embeddings.position_embeddings.weight.data = new_pos_embed
    model.roberta.embeddings.position_ids.data = torch.tensor(
        [i for i in range(max_pos)]).reshape(1, max_pos)

    # replace the `modeling_bert.BertSelfAttention` object with `LongformerSelfAttention`
    config.attention_window = [attention_window] * config.num_hidden_layers
    for i, layer in enumerate(model.roberta.encoder.layer):
        longformer_self_attn = LongformerSelfAttention(config, layer_id=i)
        longformer_self_attn.query = layer.attention.self.query
        longformer_self_attn.key = layer.attention.self.key
        longformer_self_attn.value = layer.attention.self.value

        longformer_self_attn.query_global = copy.deepcopy(
            layer.attention.self.query)
        longformer_self_attn.key_global = copy.deepcopy(
            layer.attention.self.key)
        longformer_self_attn.value_global = copy.deepcopy(
            layer.attention.self.value)

        layer.attention.self = longformer_self_attn

    logger.info(f'saving model to {save_model_to}')
    model.save_pretrained(save_model_to)
    tokenizer.save_pretrained(save_model_to)
    return model, tokenizer


def copy_proj_layers(model):
    for i, layer in enumerate(model.roberta.encoder.layer):
        layer.attention.self.query_global = copy.deepcopy(
            layer.attention.self.query)
        layer.attention.self.key_global = copy.deepcopy(
            layer.attention.self.key)
        layer.attention.self.value_global = copy.deepcopy(
            layer.attention.self.value)
    return model


def get_last_checkpoint(folder):
    PREFIX_CHECKPOINT_DIR = "checkpoint"
    _re_checkpoint = re.compile(r"^" + PREFIX_CHECKPOINT_DIR + r"\-(\d+)$")
    content = os.listdir(folder)
    checkpoints = [
        path
        for path in content
        if _re_checkpoint.search(path) is not None and os.path.isdir(os.path.join(folder, path))
    ]
    if len(checkpoints) == 0:
        return
    return os.path.join(folder, max(checkpoints, key=lambda x: int(_re_checkpoint.search(x).groups()[0])))


def pretrain_and_evaluate(args, model, tokenizer, eval_only, model_path, init_weights=False, use_roberta=False):
    val_dataset = TextDataset(tokenizer=tokenizer,
                              file_path=args.val_datapath,
                              block_size=tokenizer.max_len)
    if eval_only:
        train_dataset = val_dataset
    else:
        logger.info(
            f'Loading and tokenizing training data is usually slow: {args.train_datapath}')
        train_dataset = TextDataset(tokenizer=tokenizer,
                                    file_path=args.train_datapath,
                                    block_size=tokenizer.max_len)

    last_checkpoint = get_last_checkpoint(args.output_dir)
    if last_checkpoint is not None:
        logger.info(f'loading last checkpoint: {last_checkpoint}')
        if use_roberta:
            model = RobertaForMaskedLM.from_pretrained(last_checkpoint)
        else:
            model = RobertaLongForMaskedLM.from_pretrained(last_checkpoint)
    else:
        if init_weights:
            logger.info('initializing weights')
            model.init_weights()

    data_collator = DataCollatorForLanguageModeling(
        tokenizer=tokenizer, mlm=True, mlm_probability=0.15)
    trainer = Trainer(model=model, args=args, data_collator=data_collator,
                      train_dataset=train_dataset, eval_dataset=val_dataset, prediction_loss_only=True)

    eval_loss = trainer.evaluate()
    eval_loss = eval_loss['eval_loss']
    logger.info(f'Initial eval bpc: {eval_loss/math.log(2)}')

    if not eval_only:
        trainer.train(model_path=last_checkpoint)
        trainer.save_model()

        eval_loss = trainer.evaluate()
        eval_loss = eval_loss['eval_loss']
        logger.info(f'Eval bpc after pretraining: {eval_loss/math.log(2)}')


@dataclass
class ModelArgs:
    attention_window: int = field(
        default=512, metadata={"help": "Size of attention window"})
    max_pos: int = field(default=2048, metadata={"help": "Maximum position"})
    train_datapath: str = None
    val_datapath: str = None
    from_scratch: bool = False
    use_roberta: bool = False
    wandb_name: str = 'tmp'


def main():

    parser = HfArgumentParser((TrainingArguments, ModelArgs,))

    training_args, model_args = parser.parse_args_into_dataclasses(
        look_for_args_file=False)
    training_args.report_to = 'wandb'
    training_args.evaluate_during_training = True
    training_args.train_datapath = model_args.train_datapath
    training_args.val_datapath = model_args.val_datapath

    wandb.login()
    wandb.init(id=model_args.wandb_name)
    # Choose GPU
    # os.environ["CUDA_VISIBLE_DEVICES"] = "0,1"

    roberta_base = RobertaForMaskedLM.from_pretrained('roberta-base')
    roberta_base_tokenizer = RobertaTokenizerFast.from_pretrained(
        'roberta-base')
    # logger.info('Evaluating roberta-base (seqlen: 512) for refernece ...')
    # pretrain_and_evaluate(training_args, roberta_base, roberta_base_tokenizer, eval_only=True, model_path=None)

    model_path = f'{training_args.output_dir}/roberta-base-{model_args.max_pos}'
    if not os.path.exists(model_path):
        os.makedirs(model_path)
        logger.info(
            f'Converting roberta-base into roberta-base-{model_args.max_pos}')
        model, tokenizer = create_long_model(
            save_model_to=model_path, attention_window=model_args.attention_window, max_pos=model_args.max_pos)

    if model_args.use_roberta:
        logger.info(f'Using Roberta...')
        tokenizer = roberta_base_tokenizer
        model = roberta_base
    else:
        logger.info(f'Longformer... Loading the model from {model_path}')
        logger.info(f'Pretraining roberta-base-{model_args.max_pos} ... ')
        tokenizer = RobertaTokenizerFast.from_pretrained(model_path)
        model = RobertaLongForMaskedLM.from_pretrained(model_path)

    pretrain_and_evaluate(training_args, model, tokenizer, eval_only=False,
                          model_path=training_args.output_dir, init_weights=model_args.from_scratch, use_roberta=model_args.use_roberta)

    logger.info(
        f'Copying local projection layers into global projection layers ... ')
    model = copy_proj_layers(model)
    logger.info(f'Saving model to {model_path}')
    model.save_pretrained(model_path)

    logger.info(f'Loading the model from {model_path}')
    tokenizer = RobertaTokenizerFast.from_pretrained(model_path)
    model = RobertaLongForMaskedLM.from_pretrained(model_path)


if __name__ == "__main__":
    main()
