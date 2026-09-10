from __future__ import annotations


import httpx


class DeviceApiClient:
    """Client subtire peste API-ul public /api/v1 pentru dispozitive.
    Simulatorul foloseste EXCLUSIV acest contract public -- nu are acces
    privilegiat la baza de date a platformei."""

    def __init__(self, base_url: str, timeout: float = 20.0):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(timeout=timeout)
        self._auth_header: dict[str, str] = {}

    def set_credentials(self, device_id: str, secret: str) -> None:
        self._auth_header = {"Authorization": f"Bearer {device_id}.{secret}"}

    def claim(self, claim_code: str, device_name: str, hardware_info: dict) -> dict:
        resp = self._client.post(
            f"{self.base_url}/devices/claim",
            json={"claim_code": claim_code, "device_name": device_name, "hardware_info": hardware_info},
        )
        resp.raise_for_status()
        return resp.json()

    def heartbeat(self, boot_id: str, firmware_version: str, capabilities: dict) -> dict:
        resp = self._client.post(
            f"{self.base_url}/devices/heartbeat",
            json={"boot_id": boot_id, "firmware_version": firmware_version, "capabilities": capabilities},
            headers=self._auth_header,
        )
        resp.raise_for_status()
        return resp.json()

    def send_telemetry(self, items: list[dict]) -> dict:
        resp = self._client.post(
            f"{self.base_url}/telemetry/batch", json={"items": items}, headers=self._auth_header
        )
        resp.raise_for_status()
        return resp.json()

    def get_config(self) -> dict:
        resp = self._client.get(f"{self.base_url}/config", headers=self._auth_header)
        resp.raise_for_status()
        return resp.json()

    def get_active_plan(self) -> dict:
        resp = self._client.get(f"{self.base_url}/plan/active", headers=self._auth_header)
        resp.raise_for_status()
        return resp.json()

    def accept_plan(self, version: int) -> dict:
        resp = self._client.post(f"{self.base_url}/plan/accept", params={"version": version}, headers=self._auth_header)
        resp.raise_for_status()
        return resp.json()

    def list_pending_commands(self) -> list[dict]:
        resp = self._client.get(f"{self.base_url}/commands/pending", headers=self._auth_header)
        resp.raise_for_status()
        return resp.json()

    def ack_command(self, command_id: str, status: str, reason: str | None = None) -> dict:
        resp = self._client.post(
            f"{self.base_url}/commands/{command_id}/ack", json={"status": status, "reason": reason}, headers=self._auth_header
        )
        resp.raise_for_status()
        return resp.json()

    def report_result(self, command_id: str, status: str, details: dict, error_message: str | None = None) -> dict:
        resp = self._client.post(
            f"{self.base_url}/commands/{command_id}/result",
            json={"status": status, "details": details, "error_message": error_message},
            headers=self._auth_header,
        )
        resp.raise_for_status()
        return resp.json()

    def close(self) -> None:
        self._client.close()
