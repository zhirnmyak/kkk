from io import BytesIO
from xml.sax.saxutils import escape
import zipfile


def make_pptx(texts, image=False, reverse_order=False):
    p = "http://schemas.openxmlformats.org/presentationml/2006/main"
    a = "http://schemas.openxmlformats.org/drawingml/2006/main"
    r = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    rel = "http://schemas.openxmlformats.org/package/2006/relationships"
    buf = BytesIO()
    order = list(range(1, len(texts) + 1))
    if reverse_order:
        order.reverse()
    with zipfile.ZipFile(buf, "w") as z:
        ids = ''.join(f'<p:sldId id="{255+i}" r:id="rId{i}"/>' for i in order)
        z.writestr("ppt/presentation.xml", f'<p:presentation xmlns:p="{p}" xmlns:r="{r}"><p:sldIdLst>{ids}</p:sldIdLst></p:presentation>')
        relationships = ''.join(f'<Relationship Id="rId{i}" Type="{r}/slide" Target="slides/slide{i}.xml"/>' for i in order)
        z.writestr("ppt/_rels/presentation.xml.rels", f'<Relationships xmlns="{rel}">{relationships}</Relationships>')
        for i, text in enumerate(texts, 1):
            paragraphs = ''.join(f'<a:p><a:r><a:t>{escape(line)}</a:t></a:r></a:p>' for line in text.splitlines())
            pic = '<p:pic><p:blipFill><a:blip r:embed="pic1"/></p:blipFill></p:pic>' if image else ''
            z.writestr(f"ppt/slides/slide{i}.xml", f'<p:sld xmlns:p="{p}" xmlns:a="{a}" xmlns:r="{r}"><p:cSld><p:spTree><p:sp><p:txBody>{paragraphs}</p:txBody></p:sp>{pic}</p:spTree></p:cSld></p:sld>')
            if image:
                z.writestr(f"ppt/slides/_rels/slide{i}.xml.rels", f'<Relationships xmlns="{rel}"><Relationship Id="pic1" Type="{r}/image" Target="../media/image1.png"/></Relationships>')
        if image:
            z.writestr("ppt/media/image1.png", b"test-image-bytes")
    return buf.getvalue()
