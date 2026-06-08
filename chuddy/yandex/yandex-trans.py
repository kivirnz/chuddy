#yandex translate mobile api abuser
#written by real human stupidity + some artificial one for flavour

import requests
from random import choice
from PIL import Image, ImageDraw, ImageFont
import io
import textwrap
from urllib.parse import urlencode

import sys

fontFile = 'arial.ttf'

class TextBox:
    def __init__(self, x, y, w, h, r, g, b, originalText):
        #textbox geometry
        self.x = x
        self.y = y
        self.w = w
        self.h = h
        #text color
        self.r = r
        self.g = g
        self.b = b
        #translation shit
        self.originalText = originalText
        self.translatedText = ''
    def __str__(self):
        return "x: %s y: %s w: %s h: %s r: %s g: %s b: %s og_text: %s tr_text: %s" % (self.x, self.y, self.w, self.h, self.r, self.g, self.b, self.originalText, self.translatedText)


def add_textbox_auto_font(
    image,
    x, y,
    width, height,
    text,
    font_path=fontFile,
    max_font_size=120,
    min_font_size=14,
    text_color=(255, 255, 255),
):
    draw = ImageDraw.Draw(image)

    def get_wrapped_text(font, text, max_width):
        avg_char_width = font.getlength("A")
        max_chars = max(1, int(max_width / avg_char_width))
        return textwrap.fill(text, width=max_chars)

    best_font = None
    best_text = None
    best_size = min_font_size

    # Try from large to small font size
    for size in range(max_font_size, min_font_size - 1, -1):
        font = ImageFont.truetype(font_path, size)
        wrapped = get_wrapped_text(font, text, width)

        bbox = draw.multiline_textbbox((0, 0), wrapped, font=font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]

        if text_w <= width and text_h <= height:
            best_font = font
            best_text = wrapped
            best_size = size
            break

    # fallback if nothing fits
    if best_font is None:
        best_font = ImageFont.truetype(font_path, min_font_size)
        best_text = get_wrapped_text(best_font, text, width)

    # Measure final text
    bbox = draw.multiline_textbbox((0, 0), best_text, font=best_font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]

    # Center inside box
    text_x = x + (width - text_w) / 2
    text_y = y + (height - text_h) / 2

    draw.multiline_text(
        (text_x, text_y),
        best_text,
        font=best_font,
        fill=text_color,
        align="center"
    )

    return image

if (len(sys.argv) != 3):
    print("usage: yandex-trans.py <input file path> <output file path>")
    exit(4)
    
inputFilePath = sys.argv[1]
outputFilePath = sys.argv[2]



s = requests.Session()
s.headers.update({
    'User-Agent' : 'ru.yandex.translate/108.5.31080500 (Xiaomi Rectal Computer XXL; Android 67 (Six Seven))',
    'host' : "translate.yandex.net"
})


#OCRs image + gets a link for overlay with blurred text
#ocrUrl = "https://translate.yandex.net/ocr/v1.1/recognize?srv=android&lang=*&eraseText=true&rotate=auto&rotateColorCoordinates=true&enableVerticalLines=true"
ocrUrl = "https://translate.yandex.net/ocr/v1.1/recognize?srv=android&lang=*&eraseText=true"
with open(inputFilePath, 'rb') as f:
    file = {'file': ('file.jpg', f, 'image/jpeg')}
    r = s.post(ocrUrl, files=file)
    ocrJson = r.json()
#print(r.text)

if not (r.status_code == 200 and "status" in ocrJson and ocrJson["status"] == "success"):
    print('error getting OCR')
    exit(1)

detectedLang = ocrJson["data"]["detected_lang"]
erasedImageUrl = "https://ocrapi-text-erasure.s3.yandex.net/%s" % (ocrJson["data"]["erased_image"])

textBlocks = []

#translation shit
translationPayload = [("source_lang", detectedLang), ("target_lang", "en")]
for block in ocrJson["data"]["blocks"]:
    for box in block["boxes"]:
        translationPayload.append(("text", box["text"]))
        textBlocks.append(
            TextBox(box["x"], box["y"], box["w"], box["h"], box["textColor"]["r"], box["textColor"]["g"], box["textColor"]["b"], box["text"])
        )

hexChars = '0123456789abcdef'
bullshitId = ''.join(choice(hexChars) for i in range(32)) + "-0-0" #yep.
translateUrl = "https://translate.yandex.net/api/v1/tr.json/translate?id=%s&srv=android&format=html" % (bullshitId)
s.headers.update({
    'Content-Type' : 'application/x-www-form-urlencoded'
})
r = s.post(translateUrl, data=urlencode(translationPayload))
translationJson = r.json()
if not (r.status_code == 200 and translationJson["code"] == 200): #kys
    print('error translating')
    exit(2)

if (len(translationJson["text"]) != len(textBlocks)):
    print('shit happened during translations, ammount of blocks mismatches')
    exit(5)

for i in range(0, len(translationJson["text"])):
    textBlocks[i].translatedText = translationJson["text"][i]

s.headers.clear()
s.headers.update({
    'User-Agent' : 'okhttp/4.12.0'
})

r = s.get(erasedImageUrl)

#image processing shit
bg = Image.open(inputFilePath)
overlay = Image.open(io.BytesIO(r.content)).convert("RGBA")

overlay = overlay.resize(bg.size, Image.Resampling.LANCZOS)

bg.paste(overlay, (0, 0), overlay)

for tb in textBlocks:
    bg = add_textbox_auto_font(
        image=bg,
        x=tb.x,
        y=tb.y,
        width=tb.w,
        height=tb.h,
        text=tb.translatedText,
        text_color=(tb.r, tb.g, tb.b)
    )

bg.save(outputFilePath)
