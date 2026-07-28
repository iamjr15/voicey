from __future__ import annotations

import os
import uuid

import pytest

pytestmark = pytest.mark.live
_ACK = "I_ACKNOWLEDGE_OBJECT_STORE_MUTATION"


@pytest.mark.asyncio
async def test_live_s3_compatible_object_store_round_trip() -> None:
    if os.environ.get("VOICEKIT_LIVE_OBJECT_ACK") != _ACK:
        pytest.skip(f"set VOICEKIT_LIVE_OBJECT_ACK={_ACK}")
    bucket = os.environ.get("VOICEKIT_OBJECT_BUCKET")
    region = os.environ.get("AWS_REGION")
    if not bucket or not region:
        pytest.skip("VOICEKIT_OBJECT_BUCKET and AWS_REGION are required")

    from voicekit.storage.s3 import S3ArtifactStore

    store = S3ArtifactStore(
        bucket,
        prefix=os.environ.get("VOICEKIT_OBJECT_PREFIX", "voicekit-live-preflight"),
        endpoint_url=os.environ.get("VOICEKIT_OBJECT_ENDPOINT"),
        region_name=region,
        force_path_style=os.environ.get("VOICEKIT_OBJECT_FORCE_PATH_STYLE") == "true",
    )
    assert await store.ready()
    await store.verify_round_trip(f"live-{uuid.uuid4().hex}")
