"""Read-only visual judgments. They never grant permission to execute actions."""
from __future__ import annotations

from io import BytesIO
import math
from pathlib import Path

MODEL = "jev-1.14.0"
SCHEMA = {
    "name": "browser_facebook_vision",
    "description": (
        "Read-only experimental Jev image diagnosis of the OWNED workflow tab. "
        "Returns screen classification, popup presence and a possible Close-button box. "
        "Does not click, log in, create or change checkpoints. A viewport capture cannot "
        "show browser-chrome/native popups; retain native dialog diagnostics. "
        "Use after browser_facebook_next blocks; do not treat visual guesses as identity "
        "or creation evidence. Requires typesafe-sdk>=0.7.5, Pillow and API key."
    ),
    "inputSchema": {
        "type": "object", "additionalProperties": False,
        "properties": {"run_id": {"type": "string", "minLength": 1}},
        "required": ["run_id"],
    },
}


def prepare_image(data: bytes) -> bytes:
    """Meet API dimensions/area without changing the user's browser viewport."""
    from PIL import Image, ImageOps

    if len(data) > 16 * 1024 * 1024:
        raise ValueError("Screenshot exceeds 16 MiB")
    with Image.open(BytesIO(data)) as source:
        source.load()
        image = ImageOps.exif_transpose(source).convert("RGB")
        image.thumbnail((1568, 1568))
        # Padding keeps tiny/extreme-aspect captures within width/height/area limits.
        width, height = max(280, image.width), max(280, image.height)
        canvas = Image.new("RGB", (width, height), "white")
        canvas.paste(image, (0, 0))
        output = BytesIO()
        canvas.save(output, format="PNG")
        return output.getvalue()


def diagnose(data: bytes, *, client=None) -> dict:
    from typesafe_sdk import Box, Choice, Image, Noul, RetryPolicy, TypeSafeClient

    owns_client = client is None
    client = client or TypeSafeClient(model=MODEL, timeout=20, retry=RetryPolicy(max_retries=1))
    try:
        response = client.system_one(
            model=MODEL, state={"screen": Image(prepare_image(data))},
            questions={
                "screen_kind": Choice(
                    instructions="Classify the currently visible Facebook screen in `screen`. Choose unknown when unclear.",
                    criteria={
                        "account_chooser": "Signed-out saved-account selection screen.",
                        "password_form": "Password entry form for a selected account.",
                        "home": "Signed-in Facebook Home feed.",
                        "reels": "Facebook Reels video viewer.",
                        "creation_form": "Facebook Page creation form.",
                        "challenge": "MFA, CAPTCHA, account verification or an account restriction.",
                        "error": "An error, network failure or unavailable page.",
                        "unknown": "None of these states is clearly visible.",
                    }),
                "dismissible_popup": Noul(instructions=(
                    "Does `screen` show a Sign in as credential chooser or notification permission popup "
                    "with a visible Close/Not now control? Password entry, MFA, CAPTCHA, and account "
                    "restrictions do not count as dismissible popups.")),
                "close_box": Box(instructions=(
                    "The Close or Not now control within the Sign in as chooser or notification popup in `screen`.")),
            }, extra_headers={"TypeSafe-Danger-Zone-Images": "true"})
        raw = response.model_dump(mode="json")
        answers = raw["answers"]
        probability = float(answers["dismissible_popup"]["noul"])
        if not math.isfinite(probability) or not 0 <= probability <= 1:
            raise ValueError("Invalid visual probability")
        # Box has no absent-target result. Suppress its speculative location when
        # presence is unlikely. No threshold here authorizes a click.
        box = answers["close_box"] if probability >= 0.5 else None
        return {"model": raw.get("model", MODEL), "screen": answers["screen_kind"],
                "popup_probability": probability, "possible_close_box": box,
                "actionable": False, "experimental": True,
                "capture_scope": "page_viewport_only",
                "instruction": "Use native popup inspection and fresh DOM status before acting. Visual judgments are not identity or creation evidence."}
    finally:
        if owns_client:
            client.close()


def diagnose_file(path: str) -> dict:
    """Only trusted capture paths from the backend, never a model-supplied path."""
    with Path(path).open("rb") as stream:
        return diagnose(stream.read(16 * 1024 * 1024 + 1))
