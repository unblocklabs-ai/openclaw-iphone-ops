from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from openclaw_iphone.devicectl import App, Device
from openclaw_iphone.errors import AppNotFound
from openclaw_iphone.recipes.instagram import smoke


class InstagramSmokeTests(unittest.TestCase):
    def client(self, apps: list[App]) -> Mock:
        client = Mock()
        client.select_device.return_value = Device("Phone", "device", "connected", "iPhone")
        client.require_unlocked.return_value = Path("lock.json")
        client.coredevice_wda_url.return_value = ("http://wda.test", Path("details.json"))
        client.list_apps.return_value = (apps, Path("original-apps.json"))
        return client

    def test_smoke_uses_one_inventory_and_its_original_artifact(self) -> None:
        client = self.client([App("Instagram", "com.burbn.instagram")])
        with patch("openclaw_iphone.recipes.instagram.WDAClient"):
            result = smoke(client)
        self.assertEqual(result.apps_artifact, Path("original-apps.json"))
        client.list_apps.assert_called_once_with("device", include_all=True)
        client.launch_app.assert_called_once_with("device", "com.burbn.instagram")

    def test_smoke_rejects_ambiguous_or_wrong_bundle_without_launch(self) -> None:
        for apps, error in (([App("Instagram", "com.burbn.instagram"), App("Instagram", "other")], AppNotFound),
                            ([App("Instagram", "other")], ValueError)):
            with self.subTest(apps=apps):
                client = self.client(apps)
                with patch("openclaw_iphone.recipes.instagram.WDAClient"), self.assertRaises(error):
                    smoke(client)
                client.list_apps.assert_called_once()
                client.launch_app.assert_not_called()
