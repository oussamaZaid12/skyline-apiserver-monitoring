# Copyright 2021 99cloud
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

from sqlalchemy import JSON, Column, Integer, MetaData, String, Table, Boolean, DateTime, Float, ForeignKey, Text

METADATA = MetaData()


RevokedToken = Table(
    "revoked_token",
    METADATA,
    Column("uuid", String(length=128), nullable=False, index=True, unique=False),
    Column("expire", Integer, nullable=False),
)

Settings = Table(
    "settings",
    METADATA,
    Column("key", String(length=128), nullable=False, index=True, unique=True),
    Column("value", JSON, nullable=True),
)
AlertRules = Table(
    "alert_rules",
    METADATA,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", String(length=128), nullable=False, index=True),
    Column("name", String(length=255), nullable=False),
    Column("instance_id", String(length=64), nullable=False),
    Column("instance_name", String(length=255), nullable=False),
    Column("metric", String(length=32), nullable=False),
    Column("operator", String(length=4), nullable=False),
    Column("threshold", Float, nullable=False),
    Column("duration_seconds", Integer, nullable=False, default=60),
    Column("notify_ui", Boolean, nullable=False, default=True),
    Column("notify_email", Boolean, nullable=False, default=False),
    Column("email_address", String(length=255), nullable=True),
    Column("notify_webhook", Boolean, nullable=False, default=False),
    Column("webhook_url", Text, nullable=True),
    Column("is_active", Boolean, nullable=False, default=True),
    Column("created_at", DateTime, nullable=False),
)

AlertEvents = Table(
    "alert_events",
    METADATA,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("rule_id", Integer, ForeignKey("alert_rules.id", ondelete="CASCADE"), nullable=False, index=True),
    Column("user_id", String(length=128), nullable=False, index=True),
    Column("rule_name", String(length=255), nullable=False),
    Column("instance_id", String(length=64), nullable=False),
    Column("instance_name", String(length=255), nullable=False),
    Column("metric", String(length=32), nullable=False),
    Column("value", Float, nullable=False),
    Column("threshold", Float, nullable=False),
    Column("triggered_at", DateTime, nullable=False),
    Column("resolved_at", DateTime, nullable=True),
    Column("is_resolved", Boolean, nullable=False, default=False),
)
