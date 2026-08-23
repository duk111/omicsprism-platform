"""FIX-02: 测试同一 thread 内多次上传时文件继承机制。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from backend.app.agent.product_store import (
    AgentResourceNotFound,
    InMemoryAgentProductStore,
)
from backend.app.agent.schemas import (
    AgentInputBundleRecord,
    AgentInputFileRecord,
    AgentThreadRecord,
)


def test_consecutive_bundles_inherit_previous_files():
    """同一 thread 连续创建两个 bundle，第二个应继承第一个中未覆盖的文件角色。"""
    store = InMemoryAgentProductStore()
    now = datetime.now(timezone.utc)
    user_id = "user-test"
    thread_id = f"thread-{uuid4()}"

    # 创建 thread
    thread = AgentThreadRecord(
        thread_id=thread_id,
        user_id=user_id,
        title="Test thread",
        current_run_id=f"run-{uuid4()}",
        status="active",
        version=0,
        created_at=now,
        updated_at=now,
    )
    store.save_thread(thread)

    # 第一个 bundle: metadata + counts
    bundle1 = AgentInputBundleRecord(
        bundle_id=f"bundle-{uuid4()}",
        thread_id=thread_id,
        user_id=user_id,
        status="active",
        expires_at=now + timedelta(hours=24),
        created_at=now,
    )
    files1 = [
        AgentInputFileRecord(
            file_id=f"file-{uuid4()}",
            bundle_id=bundle1.bundle_id,
            user_id=user_id,
            field="metadata",
            filename="metadata.csv",
            storage_key="key1",
            checksum="sha256:aaa",
            content_type="text/csv",
            size_bytes=1000,
            created_at=now,
        ),
        AgentInputFileRecord(
            file_id=f"file-{uuid4()}",
            bundle_id=bundle1.bundle_id,
            user_id=user_id,
            field="counts",
            filename="counts.csv",
            storage_key="key2",
            checksum="sha256:bbb",
            content_type="text/csv",
            size_bytes=2000,
            created_at=now,
        ),
    ]
    store.save_input_bundle_with_files(bundle=bundle1, files=files1)

    # 第二个 bundle: transcriptome + group + metabolome (应继承 metadata + counts)
    now2 = now + timedelta(seconds=10)
    bundle2 = AgentInputBundleRecord(
        bundle_id=f"bundle-{uuid4()}",
        thread_id=thread_id,
        user_id=user_id,
        status="active",
        expires_at=now2 + timedelta(hours=24),
        created_at=now2,
    )

    # 模拟继承逻辑
    previous = store.get_latest_active_bundle(thread_id=thread_id, user_id=user_id, before=now2)
    assert previous is not None
    assert previous.bundle_id == bundle1.bundle_id

    prev_files = store.list_input_files(bundle_id=previous.bundle_id, user_id=user_id)
    new_fields = {"transcriptome", "group", "metabolome"}

    files2 = []
    # 继承未覆盖的字段
    for pf in prev_files:
        if pf.field not in new_fields:
            files2.append(AgentInputFileRecord(
                file_id=f"file-{uuid4()}",
                bundle_id=bundle2.bundle_id,
                user_id=user_id,
                field=pf.field,
                filename=pf.filename,
                storage_key=pf.storage_key,
                checksum=pf.checksum,
                content_type=pf.content_type,
                size_bytes=pf.size_bytes,
                created_at=now2,
            ))

    # 添加新上传的文件
    files2.extend([
        AgentInputFileRecord(
            file_id=f"file-{uuid4()}",
            bundle_id=bundle2.bundle_id,
            user_id=user_id,
            field="transcriptome",
            filename="trans.csv",
            storage_key="key3",
            checksum="sha256:ccc",
            content_type="text/csv",
            size_bytes=3000,
            created_at=now2,
        ),
        AgentInputFileRecord(
            file_id=f"file-{uuid4()}",
            bundle_id=bundle2.bundle_id,
            user_id=user_id,
            field="group",
            filename="group.csv",
            storage_key="key4",
            checksum="sha256:ddd",
            content_type="text/csv",
            size_bytes=500,
            created_at=now2,
        ),
        AgentInputFileRecord(
            file_id=f"file-{uuid4()}",
            bundle_id=bundle2.bundle_id,
            user_id=user_id,
            field="metabolome",
            filename="metab.csv",
            storage_key="key5",
            checksum="sha256:eee",
            content_type="text/csv",
            size_bytes=2500,
            created_at=now2,
        ),
    ])

    store.save_input_bundle_with_files(bundle=bundle2, files=files2)

    # 验证第二个 bundle 包含全部 5 个角色
    result_files = store.list_input_files(bundle_id=bundle2.bundle_id, user_id=user_id)
    assert len(result_files) == 5
    fields = {f.field for f in result_files}
    assert fields == {"metadata", "counts", "transcriptome", "group", "metabolome"}


def test_same_field_override_not_duplicate():
    """同角色重复上传时以新文件为准，不产生重复角色。"""
    store = InMemoryAgentProductStore()
    now = datetime.now(timezone.utc)
    user_id = "user-test"
    thread_id = f"thread-{uuid4()}"

    thread = AgentThreadRecord(
        thread_id=thread_id,
        user_id=user_id,
        title="Test thread",
        current_run_id=f"run-{uuid4()}",
        status="active",
        version=0,
        created_at=now,
        updated_at=now,
    )
    store.save_thread(thread)

    # 第一个 bundle: counts
    bundle1 = AgentInputBundleRecord(
        bundle_id=f"bundle-{uuid4()}",
        thread_id=thread_id,
        user_id=user_id,
        status="active",
        expires_at=now + timedelta(hours=24),
        created_at=now,
    )
    files1 = [
        AgentInputFileRecord(
            file_id=f"file-{uuid4()}",
            bundle_id=bundle1.bundle_id,
            user_id=user_id,
            field="counts",
            filename="counts_old.csv",
            storage_key="key1",
            checksum="sha256:old",
            content_type="text/csv",
            size_bytes=1000,
            created_at=now,
        ),
    ]
    store.save_input_bundle_with_files(bundle=bundle1, files=files1)

    # 第二个 bundle: 重新上传 counts
    now2 = now + timedelta(seconds=10)
    bundle2 = AgentInputBundleRecord(
        bundle_id=f"bundle-{uuid4()}",
        thread_id=thread_id,
        user_id=user_id,
        status="active",
        expires_at=now2 + timedelta(hours=24),
        created_at=now2,
    )

    previous = store.get_latest_active_bundle(thread_id=thread_id, user_id=user_id, before=now2)
    prev_files = store.list_input_files(bundle_id=previous.bundle_id, user_id=user_id)
    new_fields = {"counts"}

    files2 = []
    # 继承逻辑：只继承未覆盖的字段
    for pf in prev_files:
        if pf.field not in new_fields:
            files2.append(AgentInputFileRecord(
                file_id=f"file-{uuid4()}",
                bundle_id=bundle2.bundle_id,
                user_id=user_id,
                field=pf.field,
                filename=pf.filename,
                storage_key=pf.storage_key,
                checksum=pf.checksum,
                content_type=pf.content_type,
                size_bytes=pf.size_bytes,
                created_at=now2,
            ))

    # 新上传的 counts
    files2.append(AgentInputFileRecord(
        file_id=f"file-{uuid4()}",
        bundle_id=bundle2.bundle_id,
        user_id=user_id,
        field="counts",
        filename="counts_new.csv",
        storage_key="key2",
        checksum="sha256:new",
        content_type="text/csv",
        size_bytes=1500,
        created_at=now2,
    ))

    store.save_input_bundle_with_files(bundle=bundle2, files=files2)

    # 验证只有一个 counts，且是新的
    result_files = store.list_input_files(bundle_id=bundle2.bundle_id, user_id=user_id)
    assert len(result_files) == 1
    assert result_files[0].field == "counts"
    assert result_files[0].filename == "counts_new.csv"
    assert result_files[0].checksum == "sha256:new"


def test_cross_thread_no_inheritance():
    """跨 thread 不继承文件。"""
    store = InMemoryAgentProductStore()
    now = datetime.now(timezone.utc)
    user_id = "user-test"
    thread1_id = f"thread-{uuid4()}"
    thread2_id = f"thread-{uuid4()}"

    # 创建两个 thread
    for tid in [thread1_id, thread2_id]:
        store.save_thread(AgentThreadRecord(
            thread_id=tid,
            user_id=user_id,
            title="Test thread",
            current_run_id=f"run-{uuid4()}",
            status="active",
            version=0,
            created_at=now,
            updated_at=now,
        ))

    # thread1 创建 bundle
    bundle1 = AgentInputBundleRecord(
        bundle_id=f"bundle-{uuid4()}",
        thread_id=thread1_id,
        user_id=user_id,
        status="active",
        expires_at=now + timedelta(hours=24),
        created_at=now,
    )
    files1 = [
        AgentInputFileRecord(
            file_id=f"file-{uuid4()}",
            bundle_id=bundle1.bundle_id,
            user_id=user_id,
            field="counts",
            filename="counts.csv",
            storage_key="key1",
            checksum="sha256:aaa",
            content_type="text/csv",
            size_bytes=1000,
            created_at=now,
        ),
    ]
    store.save_input_bundle_with_files(bundle=bundle1, files=files1)

    # thread2 查询不应找到 thread1 的 bundle
    now2 = now + timedelta(seconds=10)
    previous = store.get_latest_active_bundle(thread_id=thread2_id, user_id=user_id, before=now2)
    assert previous is None


def test_cross_user_no_inheritance():
    """跨 user 不继承文件。"""
    store = InMemoryAgentProductStore()
    now = datetime.now(timezone.utc)
    user1_id = "user-1"
    user2_id = "user-2"
    thread_id = f"thread-{uuid4()}"

    # user1 创建 thread 和 bundle
    store.save_thread(AgentThreadRecord(
        thread_id=thread_id,
        user_id=user1_id,
        title="Test thread",
        current_run_id=f"run-{uuid4()}",
        status="active",
        version=0,
        created_at=now,
        updated_at=now,
    ))

    bundle1 = AgentInputBundleRecord(
        bundle_id=f"bundle-{uuid4()}",
        thread_id=thread_id,
        user_id=user1_id,
        status="active",
        expires_at=now + timedelta(hours=24),
        created_at=now,
    )
    files1 = [
        AgentInputFileRecord(
            file_id=f"file-{uuid4()}",
            bundle_id=bundle1.bundle_id,
            user_id=user1_id,
            field="counts",
            filename="counts.csv",
            storage_key="key1",
            checksum="sha256:aaa",
            content_type="text/csv",
            size_bytes=1000,
            created_at=now,
        ),
    ]
    store.save_input_bundle_with_files(bundle=bundle1, files=files1)

    # user2 查询应该抛 AgentResourceNotFound（因为 thread 不属于 user2）
    now2 = now + timedelta(seconds=10)
    with pytest.raises(AgentResourceNotFound):
        store.get_latest_active_bundle(thread_id=thread_id, user_id=user2_id, before=now2)


def test_expired_bundle_not_inherited():
    """过期的 bundle 不被继承。"""
    store = InMemoryAgentProductStore()
    now = datetime.now(timezone.utc)
    user_id = "user-test"
    thread_id = f"thread-{uuid4()}"

    store.save_thread(AgentThreadRecord(
        thread_id=thread_id,
        user_id=user_id,
        title="Test thread",
        current_run_id=f"run-{uuid4()}",
        status="active",
        version=0,
        created_at=now,
        updated_at=now,
    ))

    # 创建一个已经过期的 bundle
    bundle1 = AgentInputBundleRecord(
        bundle_id=f"bundle-{uuid4()}",
        thread_id=thread_id,
        user_id=user_id,
        status="active",
        expires_at=now + timedelta(hours=1),  # 1小时后过期
        created_at=now,
    )
    files1 = [
        AgentInputFileRecord(
            file_id=f"file-{uuid4()}",
            bundle_id=bundle1.bundle_id,
            user_id=user_id,
            field="counts",
            filename="counts.csv",
            storage_key="key1",
            checksum="sha256:aaa",
            content_type="text/csv",
            size_bytes=1000,
            created_at=now,
        ),
    ]
    store.save_input_bundle_with_files(bundle=bundle1, files=files1)

    # 2小时后查询（bundle 已过期）
    now2 = now + timedelta(hours=2)
    previous = store.get_latest_active_bundle(thread_id=thread_id, user_id=user_id, before=now2)
    assert previous is None
