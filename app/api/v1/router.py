from fastapi import APIRouter

from app.api.v1 import commands, devices, enrollment, plans, telemetry

api_v1_router = APIRouter()
api_v1_router.include_router(devices.router)
api_v1_router.include_router(enrollment.router)
api_v1_router.include_router(telemetry.router)
api_v1_router.include_router(plans.router)
api_v1_router.include_router(commands.router)
