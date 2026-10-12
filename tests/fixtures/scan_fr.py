"""Generates `scan-fr.pdf`: one A4 page of French text with no text layer, like a scan, read
by the OCR test (`tests/integration/test_content.py`, `SCAN_TEXT`).

The page is drawn with Pillow at 200 dpi, then made to look scanned (slightly tilted, blurred,
grainy) and saved as a PDF holding only that picture. The project it describes, “Orion”, is
fictional. Regenerate with:

    uv run python tests/fixtures/scan_fr.py
"""

import random
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

TARGET = Path(__file__).with_name("scan-fr.pdf")
DPI = 200
WIDTH, HEIGHT = 1654, 2339  # A4 at 200 dpi
MARGIN, TOP = 110, 105
FONT = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
SIZE, LINE = 28, 37  # 10 pt text, 13.3 pt lines
TEXT_WIDTH = 1250
HEADER = "Orion — FR — Document 01 — Page 01"
PARAGRAPH = (
    "Le projet Orion regroupe les notes et les documents de l'équipe. Chaque source conserve "
    "son format et son historique. Alice peut modifier les procédures ; Bob consulte les "
    "informations autorisées. Une correction de source entraîne un nouveau calcul des "
    "synthèses. Le français et l'anglais sont disponibles dans l'interface. Les chiffres "
    "restent vérifiables : 12 dossiers, 48 documents et 96 opérations. L'échéance du projet "
    "est le 30 septembre 2026. La recherche retourne des extraits accompagnés de leur "
    "provenance. Les accents doivent rester lisibles : mémoire, équipe, création, contrôle, "
    "rôle et façade."
)
DATE = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)


def wrap(text: str, font: ImageFont.FreeTypeFont) -> list[str]:
    lines, line = [], ""
    for word in text.split(" "):
        candidate = f"{line} {word}".strip()
        if line and font.getlength(candidate) > TEXT_WIDTH:
            lines.append(line)
            line = word
        else:
            line = candidate
    return [*lines, line]


def page() -> Image.Image:
    font = ImageFont.truetype(str(FONT), SIZE)
    image = Image.new("L", (WIDTH, HEIGHT), 255)
    draw = ImageDraw.Draw(image)
    y = TOP
    draw.text((MARGIN, y), HEADER, font=font, fill=20)
    y += 2 * LINE
    for number in range(1, 5):
        for line in wrap(f"{number}. {PARAGRAPH}", font):
            draw.text((MARGIN, y), line, font=font, fill=20)
            y += LINE
        y += LINE
    return image


def scanned(image: Image.Image) -> Image.Image:
    """A slight tilt, the blur of the optics, the grain of the sensor."""
    image = image.rotate(0.4, resample=Image.Resampling.BICUBIC, fillcolor=255)
    image = image.filter(ImageFilter.GaussianBlur(0.7))
    grain = Image.frombytes("L", image.size, random.Random(1).randbytes(WIDTH * HEIGHT))
    grain = grain.point(lambda value: 255 - value // 24)  # light speckles, at most -10
    return ImageChops.multiply(image, grain)


def main() -> None:
    scanned(page()).save(
        TARGET,
        "PDF",
        resolution=DPI,
        quality=70,
        title="scan-fr",
        creationDate=DATE.utctimetuple(),
        modDate=DATE.utctimetuple(),
    )
    print(f"{TARGET}: {TARGET.stat().st_size // 1024} KiB")


if __name__ == "__main__":
    main()
