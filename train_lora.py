# ref: https://huggingface.co/spaces/baulab/Erasing-Concepts-In-Diffusion/blob/main/train.py

from typing import List

import torch
from tqdm import tqdm
import wandb
import argparse
from pathlib import Path

from lora import DEFAULT_TARGET_REPLACE, LoRANetwork
import train_util
import model_util

DEVICE_CUDA = "cuda"
DDIM_STEPS = 50


# デバッグ用...
def check_requires_grad(model: torch.nn.Module):
    for name, module in list(model.named_modules())[:5]:
        if len(list(module.parameters())) > 0:
            print(f"Module: {name}")
            for name, param in list(module.named_parameters())[:2]:
                print(f"    Parameter: {name}, Requires Grad: {param.requires_grad}")


def check_training_mode(model):
    for name, module in list(model.named_modules())[:5]:
        print(f"Module: {name}, Training Mode: {module.training}")


def train(
    prompt: str,
    pretrained_model: str,
    modules: List[str],
    iterations: int,
    neutral_prompt: str = "",
    rank: int = 4,
    alpha: float = 1.0,
    negative_guidance: float = 1.0,
    lr: float = 1e-5,
    save_path: Path = Path("./output"),
    v2: bool = False,
    v_pred: bool = False,
    precision: str = "bfloat16",
    scheduler_name: str = "lms",
    enable_wandb: bool = False,
):
    if enable_wandb:
        wandb.init(project="LECO")
        wandb.config = {
            "prompt": prompt,
            "neutral_prompt": neutral_prompt,
            "pretrained_model": pretrained_model,
            "modules": modules,
            "iterations": iterations,
            "rank": rank,
            "alpha": alpha,
            "negative_guidance": negative_guidance,
            "lr": lr,
            "v2": v2,
            "v_pred": v_pred,
            "precision": precision,
            "scheduler_name": scheduler_name,
            "save_path": str(save_path),
        }

    weight_dtype = torch.float32
    if precision == "float16":
        weight_dtype = torch.float16
    elif precision == "bfloat16":
        weight_dtype = torch.bfloat16

    tokenizer, text_encoder, unet, scheduler = model_util.load_models(
        pretrained_model,
        scheduler_name=scheduler_name,
        v2=v2,
        v_pred=v_pred,
    )

    text_encoder.to(DEVICE_CUDA, dtype=weight_dtype)
    text_encoder.eval()

    unet.to(DEVICE_CUDA, dtype=weight_dtype)
    unet.eval()

    network = LoRANetwork(unet, rank=rank, multiplier=1.0, alpha=1).to(
        DEVICE_CUDA, dtype=weight_dtype
    )

    optimizer = torch.optim.AdamW(network.prepare_optimizer_params(), lr=lr)
    criteria = torch.nn.MSELoss()

    pbar = tqdm(range(iterations))

    with torch.no_grad():
        neutral_text_embeddings = train_util.get_text_embeddings(
            tokenizer, text_encoder, [""], n_imgs=1
        )
        positive_text_embeddings = train_util.get_text_embeddings(
            tokenizer, text_encoder, [prompt], n_imgs=1
        )

    del tokenizer
    del text_encoder

    torch.cuda.empty_cache()

    # debug
    print("grads: network")
    check_requires_grad(network)

    print("training mode: network")
    check_training_mode(network)

    for i in pbar:
        if enable_wandb:
            wandb.log({"iteration": i})

        with torch.no_grad():
            scheduler.set_timesteps(DDIM_STEPS, device=DEVICE_CUDA)

            optimizer.zero_grad()

            # 1 ~ 48 からランダム
            timesteps_to = torch.randint(1, DDIM_STEPS - 1, (1,)).item()

            latents = train_util.get_initial_latents(scheduler, 1, 512, 1).to(
                DEVICE_CUDA, dtype=weight_dtype
            )
            with network:
                # ちょっとデノイズされれたものが入る
                denoised_latents = train_util.diffusion(
                    unet,
                    scheduler,
                    latents,  # 単純なノイズのlatentsを渡す
                    positive_text_embeddings,
                    start_timesteps=0,
                    total_timesteps=timesteps_to,
                    guidance_scale=3,
                    # return_steps=False,
                )

            scheduler.set_timesteps(1000)

            current_timestep = scheduler.timesteps[
                int(timesteps_to * 1000 / DDIM_STEPS)
            ]

            # with network の外では空の学習しないLoRAのみを有効にする(はず...)
            positive_latents = train_util.predict_noise(
                unet,
                scheduler,
                current_timestep,
                denoised_latents,
                positive_text_embeddings,
                guidance_scale=1,
            ).to("cpu", dtype=torch.float32)
            print("positive_latents", positive_latents[0, 0, :5, :5])
            neutral_latents = train_util.predict_noise(
                unet,
                scheduler,
                current_timestep,
                denoised_latents,
                neutral_text_embeddings,
                guidance_scale=1,
            ).to("cpu", dtype=torch.float32)
            print("neutral_latents", neutral_latents[0, 0, :5, :5])

        with network:
            negative_latents = train_util.predict_noise(
                unet,
                scheduler,
                current_timestep,
                denoised_latents,
                positive_text_embeddings,
                guidance_scale=1,
            ).to("cpu", dtype=torch.float32)
            print("negative_latents", negative_latents[0, 0, :5, :5])

        positive_latents.requires_grad = False
        neutral_latents.requires_grad = False

        # FIXME: ここのロスが二回目以降nanになる (1回目も小さすぎる)
        loss = criteria(
            negative_latents,
            neutral_latents
            - (negative_guidance * (positive_latents - neutral_latents)),
        )  # loss = criteria(e_n, e_0) works the best try 5000 epochs

        pbar.set_description(f"Loss: {loss.item():.4f}")
        if enable_wandb:
            wandb.log({"loss": loss})

        loss.backward()
        optimizer.step()

    print("Saving...")

    save_path.mkdir(parents=True, exist_ok=True)

    concept_name = prompt.replace(" ", "_")

    network.save_weights(
        save_path / f"{concept_name}_last.safetensors", dtype=weight_dtype
    )

    del (
        unet,
        scheduler,
        loss,
        optimizer,
        network,
        negative_latents,
        neutral_latents,
        positive_latents,
        latents,
    )

    torch.cuda.empty_cache()

    print("Done.")


def main(args):
    prompt = args.prompt
    neutral_prompt = args.neutral_prompt
    pretrained_model = args.pretrained_model
    rank = args.rank
    alpha = args.alpha
    iterations = args.iterations
    negative_guidance = args.negative_guidance
    lr = args.lr
    save_path = Path(args.save_path).resolve()
    v2 = args.v2
    v_pred = args.v_pred
    precision = args.precision
    scheduler_name = args.scheduler_name
    enable_wandb = args.use_wandb

    train(
        prompt,
        pretrained_model,
        modules=DEFAULT_TARGET_REPLACE,
        neutral_prompt=neutral_prompt,
        iterations=iterations,
        rank=rank,
        alpha=alpha,
        negative_guidance=negative_guidance,
        lr=lr,
        save_path=save_path,
        v2=v2,
        v_pred=v_pred,
        precision=precision,
        scheduler_name=scheduler_name,
        enable_wandb=enable_wandb,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--prompt",
        required=True,
        help="Prompt of a concept to delete, emphasis, or swap.",
    )
    parser.add_argument(
        "--neutral_prompt",
        default="",
        help="Prompt of neautral condition.",
    )
    parser.add_argument(
        "--pretrained_model",
        type=str,
        required=True,
        help="Path to diffusers model or repo name",
    )
    parser.add_argument("--rank", type=int, default=4, help="rank of LoRA")
    parser.add_argument("--alpha", type=float, default=1, help="alpha of LoRA")
    parser.add_argument(
        "--iterations", type=int, default=1000, help="Number of iterations"
    )
    parser.add_argument(
        "--negative_guidance", type=float, default=1.0, help="Negative guidance"
    )
    parser.add_argument("--lr", type=float, default=1e-5, help="Learning rate")
    parser.add_argument("--save_path", default="./output", help="Path to save weights")
    parser.add_argument("--v2", action="store_true", default=False, help="Use v2 model")
    parser.add_argument(
        "--v_pred", action="store_true", default=False, help="Use v_prediction model"
    )
    parser.add_argument(
        "--precision",
        type=str,
        choices=["float32", "float16", "bfloat16"],
        default="float16",
    )
    parser.add_argument(
        "--scheduler_name",
        type=str,
        choices=["lms", "ddim", "ddpm", "euler_a"],
        default="lms",
    )
    parser.add_argument(
        "--use_wandb",
        action="store_true",
        default=False,
        help="Use wandb to logging.",
    )

    args = parser.parse_args()

    main(args)
