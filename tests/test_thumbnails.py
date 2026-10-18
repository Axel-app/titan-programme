import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from build_v2 import build, small_picture
import export_static as export
from test_pilot import NOW
import test_v2  # module import: its test cases must not run twice
import thumbnails

try:
    from PIL import Image
except ImportError:  # the run keeps publishing without Pillow; only these tests wait for it
    Image = None

A, B, C, D = (f'https://img.example/{n}.jpg' for n in 'abcd')


def picture(width, height, mode='RGB'):
    out = io.BytesIO()
    Image.new(mode, (width, height), (200, 30, 30, 255) if mode == 'RGBA' else (200, 30, 30)).save(out, 'PNG')
    return out.getvalue()


class ThumbnailTests(unittest.TestCase):
    def guide(self, root, programmes):
        source = test_v2.ExportTests.fixture(self, root)
        build(source, [test_v2.reduction('xmltvfr', 1, [dict(id='TF1.fr', names=['TF1'], country='FR', programmes=programmes)], root)], [], NOW)
        return source

    def test_the_address_is_the_sha256_of_the_guide_link(self):
        # The apps derive the same path from the same link: never change it without them.
        self.assertEqual(thumbnails.relative_path('https://img.example/a.jpg'),
                         't/0d/0d52505084684b7e7fd0e91848f6abb31af39e7c849ef121ae78f56df8b8f103.jpg')

    def test_pictures_on_air_or_within_the_hour_those_on_air_first(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = self.guide(root, [
                [NOW - 3 * 3600, NOW - 2 * 3600, 'Fini', D],
                [NOW + 1800, NOW + 3600, 'Ensuite', B],
                [NOW - 600, NOW + 1800, 'Maintenant', A],
                [NOW + 2 * 3600, NOW + 3 * 3600, 'Ce soir', C],
                [NOW + 3 * 3600, NOW + 4 * 3600, 'Rediffusion', A],
            ])
            self.assertEqual(thumbnails.wanted_pictures(source / 'v2', NOW), [A, B])
            self.assertEqual(thumbnails.wanted_pictures(source / 'v2', NOW, limit=1), [A])

    def test_pluto_gracenote_and_sfr_are_asked_for_card_sized_pictures(self):
        self.assertEqual(small_picture('https://images.pluto.tv/episodes/x/poster.jpg?fill=blur&fit=fill&fm=jpg&h=1000&q=75&w=694'),
                         'https://images.pluto.tv/episodes/x/poster.jpg?fill=blur&fit=fill&fm=jpg&h=270&q=75&w=480')
        self.assertEqual(small_picture('https://images.pluto.tv/channels/x/featured.jpg'),
                         'https://images.pluto.tv/channels/x/featured.jpg?fit=fill&fill=blur&fm=jpg&q=75&w=480&h=270')
        self.assertEqual(small_picture('http://dtil.tmsimg.com/assets/p1.jpg?w=960&h=540'), 'https://dtil.tmsimg.com/assets/p1.jpg?w=480&h=270')
        self.assertEqual(small_picture('http://static-cdn.tv.sfr.net/data/img/1.jpg'), 'https://static-cdn.tv.sfr.net/data/img/1.jpg?w=480')
        for unchanged in ('https://img1.oqee.net/a.jpg', 'http://example.com/a.png', None):
            self.assertEqual(small_picture(unchanged), unchanged)

    def test_only_public_internet_pictures_are_fetched(self):
        for url in ('http://127.0.0.1/a.jpg', 'http://169.254.169.254/latest', 'http://10.0.0.1/a.jpg',
                    'file:///etc/passwd', 'https://user:pass@img.example/a.jpg'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                thumbnails.public_address(url)

    @unittest.skipIf(Image is None, 'Pillow')
    def test_every_shape_becomes_a_small_16_9_jpeg(self):
        for width, height, mode in ((1920, 1080, 'RGBA'), (694, 1000, 'RGB'), (900, 900, 'RGB'), (244, 152, 'RGB'), (3000, 600, 'RGB')):
            with self.subTest(size=(width, height)):
                data = thumbnails.thumbnail(picture(width, height, mode))
                with Image.open(io.BytesIO(data)) as result:
                    self.assertEqual((result.format, result.size, result.mode), ('JPEG', (480, 270), 'RGB'))
                self.assertLess(len(data), 40 * 1024)
        with self.assertRaises(Exception):
            thumbnails.thumbnail(b'<html>not a picture</html>')

    @unittest.skipIf(Image is None, 'Pillow')
    def test_only_new_pictures_are_fetched_failures_wait_and_old_ones_go(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = self.guide(root, [[NOW - 600, NOW + 3000, 'Maintenant', A], [NOW + 1800, NOW + 8 * 3600, 'Film', B]])
            asked = []

            def fetch(url):
                asked.append(url)
                if url == B:
                    raise OSError('refused')
                return picture(1920, 1080)

            report = thumbnails.update(source, NOW, fetch=fetch)
            self.assertEqual((report['made'], report['failed'], report['published']), (1, 1, 1))
            self.assertTrue((source / 'thumbs' / thumbnails.relative_path(A)).is_file())
            thumbnails.update(source, NOW + 600, fetch=fetch)
            self.assertEqual(asked, [A, B])  # A is done, B waits 6 hours before another try
            # 6 hours later B is asked again, and A, over, leaves.
            report = thumbnails.update(source, NOW + 6 * 3600 + 601, fetch=fetch)
            self.assertEqual(asked, [A, B, B])
            self.assertFalse((source / 'thumbs' / thumbnails.relative_path(A)).exists())
            self.assertEqual(report['dropped'], 1)

    @unittest.skipIf(Image is None, 'Pillow')
    def test_a_light_picture_keeps_its_link_and_is_not_fetched_again_that_day(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = self.guide(root, [[NOW - 600, NOW + 3000, 'Maintenant', A]])
            asked = []

            def fetch(url):
                asked.append(url)
                return picture(533, 300)

            report = thumbnails.update(source, NOW, fetch=fetch)
            self.assertEqual((report['made'], report['light'], report['published']), (0, 1, 0))
            self.assertFalse((source / 'thumbs' / thumbnails.relative_path(A)).exists())
            thumbnails.update(source, NOW + 1800, fetch=fetch)
            self.assertEqual(asked, [A])

    @unittest.skipIf(Image is None, 'Pillow')
    def test_a_run_makes_at_most_its_share_the_soonest_first(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = self.guide(root, [[NOW + i * 600, NOW + (i + 1) * 600, f'P{i}', u] for i, u in enumerate((A, B, C))])
            thumbnails.update(source, NOW, fetch=lambda url: picture(1280, 720), new_per_run=2)
            made = sorted(p.name for p in (source / 'thumbs').glob('t/*/*.jpg'))
            self.assertEqual(made, sorted(Path(thumbnails.relative_path(u)).name for u in (A, B)))

    @unittest.skipIf(Image is None, 'Pillow')
    def test_thumbnails_are_published_next_to_the_guide_and_leave_with_their_programme(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = self.guide(root, [[NOW - 600, NOW + 3000, 'Maintenant', A]])
            thumbnails.update(source, NOW, fetch=lambda url: picture(1280, 720))
            dist = root / 'dist'
            files = export.export_static(source, dist, NOW + 60)
            published = dist / thumbnails.relative_path(A)
            self.assertTrue(published.is_file())
            self.assertIn(b'/t/*', files['_headers'])
            self.assertIn(b'image/jpeg', files['_headers'])
            thumbnails.update(source, NOW + 9 * 3600, fetch=lambda url: picture(1280, 720))
            export.export_static(source, dist, NOW + 60)
            self.assertFalse(published.exists())

    @unittest.skipIf(Image is None, 'Pillow')
    def test_the_worker_file_limit_keeps_the_programmes_on_air_first(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = self.guide(root, [[NOW + 1200, NOW + 3000, 'Ensuite', B], [NOW - 600, NOW + 1200, 'Maintenant', A]])
            thumbnails.update(source, NOW, fetch=lambda url: picture(1280, 720))
            guide = len(export.export_static(source, root / 'dist', NOW + 60))
            with patch.object(export, 'ASSET_LIMIT', guide + 1):
                export.export_static(source, root / 'dist', NOW + 60)
            self.assertTrue((root / 'dist' / thumbnails.relative_path(A)).is_file())
            self.assertFalse((root / 'dist' / thumbnails.relative_path(B)).exists())
            with patch.object(export, 'ASSET_LIMIT', 5):
                files = export.export_static(source, root / 'dist', NOW + 60)
            self.assertIn('v1/fr.json', files)
            self.assertEqual(list((root / 'dist').glob('t/*/*.jpg')), [])


if __name__ == '__main__':
    unittest.main()
