import logging
import aiohttp

class TranslatorService:
    def __init__(self) -> None:
        self._log = logging.getLogger(self.__class__.__name__)
        self._url = "https://translate.googleapis.com/translate_a/single"

    async def translate(self, text: str, target_lang: str = "en") -> str | None:
        params = {
            "client": "gtx",
            "sl": "auto",
            "tl": target_lang,
            "dt": "t",
            "q": text,
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(self._url, params=params) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
                    # Google API returns a list of translated sentences
                    # e.g. [[['Hello', 'Привет', None, None, 1]], None, 'ru', ...]
                    if data and isinstance(data, list) and isinstance(data[0], list):
                        translated_text = "".join(sentence[0] for sentence in data[0] if sentence[0])
                        return translated_text
                    return None
        except Exception as e:
            self._log.error("Translation failed: %s", e)
            return None
