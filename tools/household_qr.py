"""
household_qr — QR code generator.

WiFi/link/contact QR codes ("scan this to join our WiFi"). Returns a
PNG data-URI which Open WebUI renders as an image.
"""

import asyncio
import base64
import io

import qrcode


class Tools:
    async def generate_qr(self, content: str, label: str = "") -> str:
        """
        Generate a QR code image for any text or link.

        Use when the user wants a QR code: WiFi login, a website link,
        contact details (vCard), a text note, etc.

        :param content: The text the QR should encode. For WiFi use:
            WIFI:T:WPA;S:<network>;P:<password>;;
            For a vCard, use the full vCard text (BEGIN:VCARD … END:VCARD).
        :param label: Optional short label shown to the user.
        :return: A data-URI PNG image (rendered automatically) plus the
            encoded content for the user to verify.
        """
        content = (content or "").strip()
        if not content:
            return "Please provide the content to encode in the QR code."
        if len(content) > 1000:
            return "Content too long for a readable QR code (max ~1000 chars)."

        def _render():
            img = qrcode.make(content)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return base64.b64encode(buf.getvalue()).decode()

        try:
            b64 = await asyncio.to_thread(_render)
        except Exception as e:
            return f"QR generation failed ({e})."
        head = f"**{label}**\n\n" if label and label.strip() else ""
        return (
            f"{head}![QR](data:image/png;base64,{b64})\n\n"
            f"Encoded content: `{content[:200]}`"
            + ("…" if len(content) > 200 else "")
            + "\n_Show this QR to the user so they can scan it._"
        )
