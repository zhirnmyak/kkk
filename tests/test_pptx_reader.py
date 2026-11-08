from tests import TEST_ROOT  # set up offline isolation before app imports
import unittest
import zipfile
from io import BytesIO
from unittest import mock
import pptx_reader
from user_errors import UserNotice
from tests.pptx_fixtures import make_pptx


class PptxReaderTests(unittest.TestCase):
    def test_fifteen_slides_preserve_order_and_empty_slide(self):
        texts = [f"Слайд {i}: учебный материал о строении клетки." for i in range(15)]
        texts[3] = ""
        pages = pptx_reader.extract(make_pptx(texts))
        self.assertEqual(len(pages), 15)
        self.assertEqual(pages[3], "[страница не распознана]")
        self.assertIn("Слайд 14", pages[14])
        self.assertIn("Слайд 14", pptx_reader.extract(make_pptx(texts, reverse_order=True))[0])

    def test_sixteen_slides_rejected_before_ocr(self):
        reader = mock.Mock()
        with self.assertRaisesRegex(UserNotice, "16.*15"):
            pptx_reader.extract(make_pptx([""] * 16, image=True), reader)
        reader.assert_not_called()

    def test_image_slide_uses_optional_ocr(self):
        data = make_pptx([""], image=True)
        self.assertEqual(pptx_reader.extract(data), ["[страница не распознана]"])
        reader = mock.Mock(return_value="Распознанный текст картинки")
        self.assertEqual(pptx_reader.extract(data, reader), ["Распознанный текст картинки"])
        reader.assert_called_once_with(b"test-image-bytes")

    def test_invalid_file_and_empty_deck_have_clear_errors(self):
        with self.assertRaisesRegex(UserNotice, "PPTX"):
            pptx_reader.extract(b"not a presentation")
        with self.assertRaisesRegex(UserNotice, "нет слайдов"):
            pptx_reader.extract(make_pptx([]))

    def test_table_cells_and_chart_cached_values_are_read(self):
        source = make_pptx(["Заголовок"])
        target = BytesIO()
        with zipfile.ZipFile(BytesIO(source)) as original, zipfile.ZipFile(target, "w") as result:
            for name in original.namelist():
                data = original.read(name)
                if name == "ppt/slides/slide1.xml":
                    extra = (f'<p:graphicFrame><a:graphic><a:graphicData><a:tbl><a:tr><a:tc>'
                             '<a:txBody><a:p><a:r><a:t>Клеточная мембрана</a:t></a:r></a:p></a:txBody>'
                             f'</a:tc></a:tr></a:tbl><c:chart xmlns:c="{pptx_reader.C}" r:id="chart1"/>'
                             '</a:graphicData></a:graphic></p:graphicFrame>')
                    data = data.decode().replace('</p:spTree>', extra + '</p:spTree>').encode()
                result.writestr(name, data)
            result.writestr('ppt/slides/_rels/slide1.xml.rels',
                            '<Relationships><Relationship Id="chart1" Target="../charts/chart1.xml"/></Relationships>')
            result.writestr('ppt/charts/chart1.xml', f'<c:chartSpace xmlns:c="{pptx_reader.C}"><c:v>Митоз</c:v><c:v>42</c:v></c:chartSpace>')
        text = pptx_reader.extract(target.getvalue())[0]
        self.assertIn("Клеточная мембрана", text)
        self.assertIn("Митоз; 42", text)
