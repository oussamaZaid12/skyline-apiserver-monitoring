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

from fastapi import APIRouter
from skyline_apiserver.api.v1 import (
    contrib,
    extension,
    instance_monitoring,
    login,
    policy,
    prometheus,
    setting,
    vm_ranking,
    instance_history,
    ai_agent,
    alert_rules,
)
api_router = APIRouter()
api_router.include_router(login.router, tags=["Login"])
api_router.include_router(extension.router, tags=["Extension"])
api_router.include_router(prometheus.router, tags=["Prometheus"])
api_router.include_router(contrib.router, tags=["Contrib"])
api_router.include_router(policy.router, tags=["Policy"])
api_router.include_router(setting.router, tags=["Setting"])
api_router.include_router(instance_monitoring.router, tags=["Instance Monitoring"])
api_router.include_router(vm_ranking.router, tags=["VM Ranking"])
api_router.include_router(instance_history.router, tags=["Instance History"])
api_router.include_router(ai_agent.router, tags=["AI Agent"])
api_router.include_router(alert_rules.router, tags=["Alerts"])
