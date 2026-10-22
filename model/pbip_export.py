from __future__ import annotations

import io
import json
import uuid
import zipfile

import requests

from auth.token_manager import get_fabric_token


def _build_pbip_zip(
    model_name: str, tmdl_files: dict[str, str], server_fqdn: str, db_name: str
) -> bytes:
    buf = io.BytesIO()
    sm_lid = str(uuid.uuid4())
    rpt_lid = str(uuid.uuid4())
    schema = "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json"
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            f"{model_name}.pbip",
            json.dumps(
                {
                    "version": "1.0",
                    "artifacts": [{"report": {"path": f"{model_name}.Report"}}],
                    "settings": {},
                },
                indent=2,
            ),
        )
        sm = f"{model_name}.SemanticModel"
        zf.writestr(
            f"{sm}/.platform",
            json.dumps(
                {
                    "$schema": schema,
                    "metadata": {"type": "SemanticModel", "displayName": model_name},
                    "config": {"version": "2.0", "logicalId": sm_lid},
                },
                indent=2,
            ),
        )
        zf.writestr(
            f"{sm}/definition.pbism",
            json.dumps({"version": "4.1", "settings": {}}, indent=2),
        )
        for rel_path, content in tmdl_files.items():
            filled = content.replace(
                "SERVER_FQDN",
                server_fqdn or "placeholder.database.fabric.microsoft.com",
            )
            filled = filled.replace("DB_NAME", db_name or "PlaceholderDB")
            zf.writestr(f"{sm}/definition/{rel_path}", filled + "\n")
        rpt = f"{model_name}.Report"
        zf.writestr(
            f"{rpt}/.platform",
            json.dumps(
                {
                    "$schema": schema,
                    "metadata": {"type": "Report", "displayName": model_name},
                    "config": {"version": "2.0", "logicalId": rpt_lid},
                },
                indent=2,
            ),
        )
        zf.writestr(
            f"{rpt}/definition.pbir",
            json.dumps(
                {
                    "version": "4.0",
                    "datasetReference": {
                        "byPath": {"path": f"../{model_name}.SemanticModel"}
                    },
                },
                indent=2,
            ),
        )
    return buf.getvalue()


def _save_pbip_to_disk(
    output_base: str,
    model_name: str,
    tmdl_files: dict[str, str],
    server_fqdn: str,
    db_name: str,
) -> str:
    from semantic_model_v2.pipeline import PipelineResult, save_pipeline_output

    return save_pipeline_output(
        PipelineResult(plan=None, plan_dict=None, tmdl_files=tmdl_files),
        output_base,
        model_name,
        server_fqdn=server_fqdn,
        db_name=db_name,
    )


def deploy_to_fabric(
    workspace_id: str,
    model_name: str,
    tmdl_files: dict[str, str],
    server_fqdn: str,
    db_name: str,
) -> dict:
    import base64
    import time

    token = get_fabric_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    base_url = f"https://api.fabric.microsoft.com/v1/workspaces/{workspace_id}/items"

    parts = []

    def _add_part(path: str, content: str):
        parts.append(
            {
                "path": path,
                "payload": base64.b64encode(content.encode("utf-8")).decode("ascii"),
                "payloadType": "InlineBase64",
            }
        )

    _add_part(
        "definition.pbism", json.dumps({"version": "4.1", "settings": {}}, indent=2)
    )

    for rel_path, content in tmdl_files.items():
        filled = content.replace(
            "SERVER_FQDN", server_fqdn or "placeholder.database.fabric.microsoft.com"
        )
        filled = filled.replace("DB_NAME", db_name or "PlaceholderDB")
        _add_part(f"definition/{rel_path}", filled)

    payload = {
        "displayName": model_name,
        "type": "SemanticModel",
        "definition": {"parts": parts},
    }

    existing_id = ""
    try:
        listing = requests.get(base_url, headers=headers, timeout=30)
        if listing.status_code == 200:
            for it in listing.json().get("value", []):
                if (
                    it.get("displayName") == model_name
                    and it.get("type") == "SemanticModel"
                ):
                    existing_id = it.get("id", "")
                    break
    except Exception:
        pass

    if existing_id:
        resp = requests.post(
            f"{base_url}/{existing_id}/updateDefinition",
            headers=headers,
            json={"definition": {"parts": parts}},
            timeout=60,
        )
        verb = "updated"
    else:
        resp = requests.post(base_url, headers=headers, json=payload, timeout=60)
        verb = "created"

    if resp.status_code in (200, 201):
        try:
            item_id = resp.json().get("id", "") or existing_id
        except Exception:
            item_id = existing_id
        return {
            "success": True,
            "item_id": item_id,
            "message": f"'{model_name}' {verb} in Fabric.",
        }

    if resp.status_code == 202:
        op_url = resp.headers.get("Location") or resp.headers.get(
            "x-ms-operation-id", ""
        )
        if not op_url:
            return {
                "success": False,
                "pending": True,
                "item_id": existing_id,
                "message": (
                    f"Deployment submitted ({verb}, async) - status not confirmed. "
                    "Check the Fabric portal."
                ),
            }

        if not op_url.startswith("http"):
            op_url = f"https://api.fabric.microsoft.com/v1/operations/{op_url}"

        for _ in range(30):
            time.sleep(2)
            poll = requests.get(op_url, headers=headers, timeout=30)
            if poll.status_code != 200:
                break
            state = poll.json()
            status = state.get("status", "")
            if status == "Succeeded":
                result_url = (
                    state.get("createdItemId")
                    or state.get("result", {}).get("id", "")
                    or existing_id
                )
                return {
                    "success": True,
                    "item_id": result_url,
                    "message": f"'{model_name}' {verb} in Fabric.",
                }
            if status in ("Failed", "Cancelled"):
                err = state.get("error", {}).get("message", str(state))
                return {
                    "success": False,
                    "item_id": "",
                    "message": f"Deployment failed: {err}",
                }

        return {
            "success": False,
            "pending": True,
            "item_id": existing_id,
            "message": (
                "Deployment still running after ~60s — status not confirmed. "
                "Check the Fabric portal."
            ),
        }

    try:
        err_msg = resp.json().get("message") or resp.json().get("error", {}).get(
            "message", resp.text[:300]
        )
    except Exception:
        err_msg = resp.text[:300]
    return {
        "success": False,
        "item_id": "",
        "message": f"HTTP {resp.status_code}: {err_msg}",
    }
