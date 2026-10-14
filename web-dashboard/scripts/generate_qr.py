"""Generate printable QR stickers for a range of item identifiers.

Each sticker encodes the full item URL, so scanning it with any camera app
opens FreshGuard rather than showing a bare code. The identifier is printed
underneath so a sticker stays usable when the camera will not cooperate.

Usage:
    python scripts/generate_qr.py --count 24 --host http://freshguard.local:5000
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - convenience for direct runs
    sys.path.insert(0, str(REPO_ROOT))

logger = logging.getLogger(__name__)

DEFAULT_OUTPUT = REPO_ROOT / "scripts" / "out" / "qr"

#: Sheet layout, in pixels at roughly 150 dpi.
COLUMNS = 4
ROWS = 6
CELL = 260
LABEL_HEIGHT = 34
MARGIN = 40


def identifier(number: int) -> str:
    """Return the identifier for a sticker number, for example FG-0007."""
    if not 0 <= number <= 9999:  # noqa: PLR2004 - four digits by definition
        raise ValueError("identifiers run from FG-0000 to FG-9999")
    return f"FG-{number:04d}"


def target_url(host: str, item_id: str) -> str:
    """The URL the sticker encodes."""
    return f"{host.rstrip('/')}/item/{item_id}"


def render_code(item_id: str, host: str, box_size: int = 8):
    """Return the QR image for one identifier."""
    import qrcode

    code = qrcode.QRCode(box_size=box_size, border=2)
    code.add_data(target_url(host, item_id))
    code.make(fit=True)
    return code.make_image(fill_color="black", back_color="white").convert("RGB")


def write_singles(identifiers: list[str], host: str, output: Path) -> list[Path]:
    """Write one PNG per identifier and return the paths."""
    output.mkdir(parents=True, exist_ok=True)
    written = []
    for item_id in identifiers:
        path = output / f"{item_id}.png"
        render_code(item_id, host).save(path)
        written.append(path)
    return written


def write_sheet(identifiers: list[str], host: str, output: Path) -> list[Path]:
    """Write printable sheets of stickers, each labelled with its identifier."""
    from PIL import Image, ImageDraw

    output.mkdir(parents=True, exist_ok=True)
    per_sheet = COLUMNS * ROWS
    sheets = []

    for index in range(0, len(identifiers), per_sheet):
        chunk = identifiers[index : index + per_sheet]
        width = MARGIN * 2 + COLUMNS * CELL
        height = MARGIN * 2 + ROWS * (CELL + LABEL_HEIGHT)
        sheet = Image.new("RGB", (width, height), "white")
        draw = ImageDraw.Draw(sheet)

        for position, item_id in enumerate(chunk):
            column = position % COLUMNS
            row = position // COLUMNS
            x = MARGIN + column * CELL
            y = MARGIN + row * (CELL + LABEL_HEIGHT)

            code = render_code(item_id, host).resize((CELL - 20, CELL - 20))
            sheet.paste(code, (x + 10, y))
            draw.text((x + 10, y + CELL - 6), item_id, fill="black")

        path = output / f"sheet-{index // per_sheet + 1:02d}.png"
        sheet.save(path)
        sheets.append(path)

    return sheets


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=int, default=1, help="first sticker number")
    parser.add_argument("--count", type=int, default=24, help="how many stickers")
    parser.add_argument(
        "--host",
        default="http://freshguard.local:5000",
        help="base URL the stickers point at",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--no-sheet", action="store_true", help="skip the printable sheets")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO)
    identifiers = [identifier(number) for number in range(args.start, args.start + args.count)]

    singles = write_singles(identifiers, args.host, args.output)
    print(f"wrote {len(singles)} sticker(s) to {args.output}")

    if not args.no_sheet:
        sheets = write_sheet(identifiers, args.host, args.output)
        print(f"wrote {len(sheets)} printable sheet(s)")

    print(f"stickers point at {target_url(args.host, identifiers[0])}")
    return 0


if __name__ == "__main__":  # pragma: no cover - script entry point
    raise SystemExit(main())
