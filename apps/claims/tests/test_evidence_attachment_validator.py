"""validate_evidence_attachment — the dispute-submission validator that allows
images AND PDFs (claim evidence stays image-only via validate_evidence_image)."""

import io

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase

from apps.claims.services import (EVIDENCE_MAX_BYTES,
                                  validate_evidence_attachment)


def _png_bytes():
    from PIL import Image
    b = io.BytesIO()
    Image.new('RGB', (8, 8), 'white').save(b, format='PNG')
    return b.getvalue()


class EvidenceAttachmentValidatorTests(SimpleTestCase):
    def test_accepts_real_pdf(self):
        f = SimpleUploadedFile('doc.pdf', b'%PDF-1.7\nbody', content_type='application/pdf')
        validate_evidence_attachment(f)  # must not raise

    def test_accepts_image(self):
        f = SimpleUploadedFile('shot.png', _png_bytes(), content_type='image/png')
        validate_evidence_attachment(f)  # must not raise

    def test_rejects_pdf_extension_with_fake_bytes(self):
        f = SimpleUploadedFile('evil.pdf', b'NOPE not a pdf', content_type='application/pdf')
        with self.assertRaises(ValidationError):
            validate_evidence_attachment(f)

    def test_rejects_unsupported_type(self):
        f = SimpleUploadedFile('a.docx', b'PK\x03\x04', content_type='application/octet-stream')
        with self.assertRaises(ValidationError):
            validate_evidence_attachment(f)

    def test_rejects_oversize_pdf(self):
        big = b'%PDF-1.4' + b'0' * (EVIDENCE_MAX_BYTES + 1)
        f = SimpleUploadedFile('big.pdf', big, content_type='application/pdf')
        with self.assertRaises(ValidationError):
            validate_evidence_attachment(f)
