"""
household_vision — Vision/photo reading helper.

Attached images are handled natively by the Gemini model (file/vision
capability). This tool standardizes what the model should extract from a
photo: labels, menus, notes, screenshots.
"""


class Tools:
    async def describe_attached_image(self, detail: str = "general") -> str:
        """
        Analyze an attached photo or image and extract its useful content.

        Use when the user attaches a picture and asks what it says, what it
        is, or asks you to read a label, menu, note, receipt or screenshot.

        :param detail: What to focus on: label, menu, note, receipt,
            screenshot, handwriting, or general.
        :return: Guidance on how to describe the attached image.
        """
        focus = (detail or "general").strip().lower()
        guidance = {
            "label": (
                "Read the label: product name, brand, net weight, ingredients "
                "(especially allergens), nutrition per 100 g/ml, best-before/expiry date. "
                "Translate non-English text for the user."
            ),
            "menu": (
                "Read the menu: dish names, prices, specials. Translate items and "
                "briefly describe what each dish is, then ask which one the user wants."
            ),
            "note": (
                "Transcribe the note verbatim (or as close as possible), then summarize "
                "the key points. Flag anything unclear or ambiguous."
            ),
            "receipt": (
                "Read the receipt: store, date, line items with prices, total, and "
                "payment method. Offer to record the total in the household ledger."
            ),
            "screenshot": (
                "Describe what the screenshot shows, then extract the relevant text "
                "and answer the user's implied question about it."
            ),
            "handwriting": (
                "Transcribe the handwriting as best you can, noting uncertainty, then "
                "summarize the content."
            ),
        }
        base = guidance.get(
            focus,
            "Describe the image in detail, then extract whatever text it contains and answer the user's question.",
        )
        return (
            f"The user attached an image (focus: {focus}). {base} "
            "Be specific and structured; keep the answer in the user's language."
        )
