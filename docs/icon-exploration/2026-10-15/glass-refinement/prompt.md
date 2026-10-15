# 内置 image_gen 编辑提示词

Use case: precise-object-edit.
Edit target: the attached desktop app icon that the user prefers. Preserve its composition and identity: three rounded-square source nodes across the top (blue, violet, blue), three smooth channels converging into a V junction, a rounded blue-violet gateway tile below, and two white code chevrons on that gateway. Keep a centered front view with no perspective tilt. Preserve relative shapes, spacing and proportions. Do not simplify into a flat vector logo.

User request: make the GLASS material substantially more distinctive and premium; the current surfaces feel plain and somewhat like opaque plastic.
Render the same icon with pronounced but elegant optical glass:
- thick, translucent blue and violet cast glass for the nodes and the connecting channels; clear polished bevels and crisp narrow rim highlights that reveal real thickness;
- softly luminous tinted interiors with visible light transmission and subtle internal refraction, with darker blue/violet edges and gentle caustic light underneath;
- a deeper blue-violet smoked-glass gateway tile, layered transparent depth, white code chevrons clearly legible on its face;
- a pale cool frosted-glass backing tile, smooth quiet center and a well-defined translucent glass rim; a subtle restrained light reflection across its upper edge;
- coherent upper-left studio lighting, controlled contrast, sharper material detail, blue/violet identity matching an airy desktop utility theme. The glass effect should be obvious at first glance without becoming flashy.
Use blue #3562b5 and violet #6952b5 as palette anchors. Keep the pale blue-white backing and high visual clarity. Avoid puffy plastic, rubber, chrome metal, neon, rainbow effects, particles, extra decoration and busy reflections.

IMPORTANT alpha cleanup: the input has accidental white ragged fragments and tiny colored speckles outside the rounded tile. These are defects, NOT part of the design. Re-render a single immaculate smooth rounded-rectangle outer silhouette. All exterior margins and corners must be truly transparent alpha zero, with only a thin smooth antialiased edge and no outside shadow, halo, stray dot, fragment or torn-paper texture. Do not draw a black background, checkerboard, scenery or framing.
One square 1024x1024 RGBA icon, safe transparent margin around the backing tile. No text, letters, numbers, watermark or added logos. Keep the existing code chevrons as the central symbol. Readable at 32px.
