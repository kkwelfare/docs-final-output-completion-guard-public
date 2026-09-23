"""Body-free CJK locale proxies from PDF bytes; never visual perception.

Local font selection is current selector evidence, not producer provenance.
Font-name matches and embedded mapping are proxies, not proof of glyph shape.
"""
from __future__ import annotations
import hashlib
import re
from pathlib import Path
import fitz
from . import cjk_font_selector

CRITERION = 'cjk_glyph_locale'
RULE = 'cjk_glyph_or_region_appearance_anomaly_candidate'
CHOICES = ['japanese_glyph_likely', 'non_japanese_glyph_likely', 'uncertain']


def sha(data):
    return hashlib.sha256(data).hexdigest()


def normalized(name):
    return ''.join(c.lower() for c in re.sub(r'^[A-Z]{6}\+', '', name) if c.isalnum())


def is_cjk(c):
    n = ord(c)
    return (0x3040 <= n <= 0x30ff or 0x3400 <= n <= 0x9fff or
            0xf900 <= n <= 0xfaff or 0x20000 <= n <= 0x323af)


def font_records(doc, page, selection):
    result = []
    for xref, ext, kind, name, resource, encoding, *_ in page.get_fonts(full=True):
        if any(r['xref'] == xref for r in result):
            continue
        if len(result) >= 32:
            raise ValueError('insufficient_evidence')
        extracted_name, _, _, data = doc.extract_font(xref)
        # Type0 ToUnicode and descendant descriptor presence are mapping proxies.
        unicode_kind, unicode_value = doc.xref_get_key(xref, 'ToUnicode')
        descendant_kind, descendant_value = doc.xref_get_key(xref, 'DescendantFonts')
        descendant = re.search(r'(\d+)\s+0\s+R', descendant_value) if descendant_kind == 'array' else None
        font_xref = int(descendant.group(1)) if descendant else xref
        descriptor_kind, descriptor_value = doc.xref_get_key(font_xref, 'FontDescriptor')
        flags = 0
        if descriptor_kind == 'xref':
            flag_kind, flag_value = doc.xref_get_key(int(descriptor_value.split()[0]), 'Flags')
            if flag_kind == 'int':
                flags = int(flag_value)
        names = {normalized(name), normalized(extracted_name)}
        selected_names = {normalized(selection.family)} if selection.family else set()
        if selection.path:
            try:
                selected_names.add(normalized(fitz.Font(fontfile=selection.path).name))
            except Exception:
                pass
        aliases = {normalized(a) for _, _, aa in cjk_font_selector.JAPANESE_FONT_GROUPS for a in aa}
        jp = any(any(n == a or n.startswith(a + 'regular') or n.startswith(a + 'bold') for a in aliases) for n in names)
        other = any(any(marker in n for marker in ('cjksc', 'cjktc', 'cjkkr', 'sanssc', 'sanstc', 'serifsc', 'seriftc')) for n in names)
        result.append({'xref':xref, 'name_sha256':sha(name.encode()),
            'embedded_sha256':sha(data) if data else None, 'embedded':bool(data),
            'to_unicode_present':unicode_kind == 'xref', 'descriptor_flags':flags,
            'selector_name_match':bool(names & selected_names),
            'selector_file_match':bool(data and sha(data) == selection.sha256),
            'japanese_name_proxy':jp, 'non_japanese_name_proxy':other,
            '_names':names})
    return result


def measure(artifact: Path, page_number: int, bbox: list, render: dict) -> dict:
    selection = cjk_font_selector.select_cjk_font()
    selected = {'available':selection.status == 'ud-installed',
        'family_sha256':sha(selection.family.encode()) if selection.family else None,
        'file_sha256':selection.sha256 or None,
        'selector_sha256':sha(Path(cjk_font_selector.__file__).read_bytes()),
        'priority':selection.priority, 'fallback_decision':selection.fallback_decision,
        'basis':'current_local_selector_not_producer_attestation'}
    with fitz.open(artifact) as doc:
        if not doc.is_pdf or not 1 <= page_number <= len(doc):
            raise ValueError('invalid_request')
        page = doc[page_number - 1]
        if page.rotation:
            raise ValueError('invalid_request')
        # Local import avoids a module cycle; preserve original bbox for binding.
        from .jev_post_render import normalize_pdf_region
        region, _ = normalize_pdf_region(page.rect, bbox)
        # Verify the supplied full-page render by decoded samples, not PNG encoding.
        rp = Path(render['path'])
        if rp.stat().st_size > 32_000_000:
            raise ValueError('insufficient_evidence')
        actual = fitz.Pixmap(str(rp))
        scale = render.get('render_scale', actual.width / page.rect.width)
        if not .25 <= scale <= 4 or actual.width * actual.height > 16_000_000:
            raise ValueError('insufficient_evidence')
        expected = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
        if (actual.width, actual.height, actual.n, sha(actual.samples)) != (expected.width, expected.height, expected.n, sha(expected.samples)):
            raise ValueError('identity_drift')
        if len(render) > 2:
            expected_meta = {'page':page_number, 'width':expected.width, 'height':expected.height,
                'render_scale':scale, 'bbox_pdf_points':bbox,
                'bbox_render_pixels':[round(v * scale, 3) for v in bbox]}
            if any(render.get(k) != v for k,v in expected_meta.items()):
                raise ValueError('identity_drift')
        fonts = font_records(doc, page, selection)
        glyphs = []; count = 0; replacement = 0
        for block in page.get_text('rawdict')['blocks']:
            for line in block.get('lines', []):
                for span in line['spans']:
                    for char in span['chars']:
                        box = fitz.Rect(char['bbox'])
                        if not box.intersects(region):
                            continue
                        count += 1
                        replacement += char['c'] == '\ufffd'
                        if not is_cjk(char['c']):
                            continue
                        matches = [f for f in fonts if normalized(span['font']) in f['_names']]
                        glyphs.append({'bbox':[round(v,4) for v in box],
                            'contained':region.contains(box), 'span_flags':int(span['flags']),
                            'font_xref':matches[0]['xref'] if len(matches) == 1 else 0})
                        if len(glyphs) > 512:
                            raise ValueError('insufficient_evidence')
        # Numeric raster summaries remain local-derived proxies. No pixels leave.
        if region.width * region.height > 1_000_000:
            raise ValueError('insufficient_evidence')
        pix = page.get_pixmap(clip=region, colorspace=fitz.csGRAY, alpha=False)
        samples = pix.samples
        raster = {'width':pix.width, 'height':pix.height,
            'nonwhite_ratio':round(sum(v < 245 for v in samples) / len(samples),6),
            'gray_mean':round(sum(samples) / len(samples),4),
            'samples_sha256':sha(samples), 'basis':'local_grayscale_1x_no_pixels_transmitted'}
        used = {g['font_xref'] for g in glyphs}
        mapped = [f for f in fonts if f['xref'] in used]
        sufficient = bool(glyphs and not replacement and 0 not in used and
            all(g['contained'] for g in glyphs) and selected['available'] and
            all(f['embedded'] and f['to_unicode_present'] for f in mapped) and
            all(f['selector_name_match'] or f['selector_file_match'] or
                f['japanese_name_proxy'] or f['non_japanese_name_proxy'] for f in mapped) and
            raster['nonwhite_ratio'] > 0)
        for f in fonts:
            del f['_names']
        return {'basis':'pdf_bytes_and_local_render_proxies_not_visual_perception',
            'text_layer_character_count':count, 'cjk_glyph_count':len(glyphs),
            'replacement_character_count':replacement, 'glyphs':glyphs, 'fonts':fonts,
            'selector':selected, 'render_verified':True, 'render_samples_sha256':sha(actual.samples),
            'local_render':raster, 'evidence_sufficient':sufficient}
