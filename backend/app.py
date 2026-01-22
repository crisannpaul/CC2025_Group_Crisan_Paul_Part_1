from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional, Tuple, Dict, Any
import os
import time
import logging
import json

import jwt
from jwt import PyJWKClient

from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient

# ----------------------------
# App + logging
# ----------------------------
app = FastAPI(title="Energy API", version="1.0.0")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("energy-api")

# ----------------------------
# CORS
# ----------------------------
origins = [
    "https://kind-dune-0fa1d2103.3.azurestaticapps.net",
    # keep local origins if you ever need them again
    "http://localhost:8080",
    "http://127.0.0.1:8080",
    "http://localhost:5500",
    "http://127.0.0.1:5500",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ----------------------------
# Cognito JWT validation config
# ----------------------------
COGNITO_REGION = os.getenv("COGNITO_REGION", "eu-central-1")
COGNITO_USER_POOL_ID = os.getenv("COGNITO_USER_POOL_ID", "eu-central-1_FgjFNIA5z")
COGNITO_APP_CLIENT_ID = os.getenv("COGNITO_APP_CLIENT_ID", "826c2fnsp719oaqrs5gttb20m")

ISSUER = f"https://cognito-idp.{COGNITO_REGION}.amazonaws.com/{COGNITO_USER_POOL_ID}"
JWKS_URL = f"{ISSUER}/.well-known/jwks.json"
_jwk_client = PyJWKClient(JWKS_URL)

logger.info("Cognito ISSUER=%s", ISSUER)
logger.info("Cognito JWKS_URL=%s", JWKS_URL)
logger.info("Cognito APP_CLIENT_ID=%s", COGNITO_APP_CLIENT_ID)

def _get_bearer_token(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    return auth.split(" ", 1)[1].strip()

def verify_cognito_jwt(token: str) -> dict:
    """
    Works for Cognito ID token OR access token.
    We disable PyJWT audience validation and check aud/client_id ourselves
    based on token_use to avoid InvalidAudienceError surprises.
    """
    try:
        unverified = jwt.decode(token, options={"verify_signature": False})
        logger.info(
            "UNVERIFIED token_use=%s iss=%s aud=%s client_id=%s",
            unverified.get("token_use"),
            unverified.get("iss"),
            unverified.get("aud"),
            unverified.get("client_id"),
        )

        signing_key = _jwk_client.get_signing_key_from_jwt(token).key
        claims = jwt.decode(
            token,
            signing_key,
            algorithms=["RS256"],
            issuer=ISSUER,
            options={
                "require": ["exp", "iat", "iss"],
                "verify_aud": False,
            },
        )

        token_use = claims.get("token_use")
        if token_use == "id":
            if claims.get("aud") != COGNITO_APP_CLIENT_ID:
                raise HTTPException(status_code=401, detail="aud mismatch")
        elif token_use == "access":
            if claims.get("client_id") != COGNITO_APP_CLIENT_ID:
                raise HTTPException(status_code=401, detail="client_id mismatch")
        else:
            raise HTTPException(status_code=401, detail="Invalid token_use")

        return claims

    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidIssuerError:
        raise HTTPException(status_code=401, detail="Invalid issuer")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("JWT verify failed: %r", e)
        raise HTTPException(status_code=401, detail="Invalid token")

def _resolve_role_and_device(claims: dict) -> Tuple[str, Optional[str]]:
    groups = claims.get("cognito:groups") or []
    if isinstance(groups, str):
        groups = [groups]

    role = "admin" if "admin" in groups else "user"
    device_id = claims.get("custom:device_id")  # may be None for admin

    logger.info(
        "RBAC_RESOLVE sub=%s token_use=%s groups=%s role=%s device_id=%s",
        claims.get("sub"),
        claims.get("token_use"),
        groups,
        role,
        device_id,
    )
    return role, device_id

def require_auth(request: Request) -> dict:
    token = _get_bearer_token(request)
    claims = verify_cognito_jwt(token)
    role, device_id = _resolve_role_and_device(claims)

    logger.info(
        "AUTH_OK method=%s path=%s sub=%s role=%s device_id=%s",
        request.method,
        request.url.path,
        claims.get("sub"),
        role,
        device_id,
    )
    return claims

# ----------------------------
# Azure Blob Storage config (ETL output)
# ----------------------------
STORAGE_ACCOUNT_NAME = os.getenv("STORAGE_ACCOUNT_NAME", "numedisponibil")
STORAGE_CONTAINER = os.getenv("STORAGE_CONTAINER", "processed")
LATEST_PREFIX = os.getenv("LATEST_PREFIX", "latest/")  # inside processed container

STORAGE_ACCOUNT_URL = os.getenv(
    "STORAGE_ACCOUNT_URL",
    f"https://{STORAGE_ACCOUNT_NAME}.blob.core.windows.net",
)

logger.info("Storage account url=%s container=%s prefix=%s",
            STORAGE_ACCOUNT_URL, STORAGE_CONTAINER, LATEST_PREFIX)

_credential = DefaultAzureCredential(exclude_interactive_browser_credential=True)
_blob_service = BlobServiceClient(account_url=STORAGE_ACCOUNT_URL, credential=_credential)
_container = _blob_service.get_container_client(STORAGE_CONTAINER)

# Simple cache so the first deploy isn’t hammered by list operations
_CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "30"))
_cache: Dict[str, Any] = {"ts": 0.0, "latest_docs": None}

# ----------------------------
# Models for new ETL schema
# ----------------------------
class EnergyRecord(BaseModel):
    timestamp: str
    kwh: float
    location: Optional[str] = None

class EnergySnapshot(BaseModel):
    device_id: str
    generation_time: Optional[str] = None
    time_window: Optional[str] = None
    file_history: Optional[str] = None
    records: List[EnergyRecord] = []
    summary: Optional[Dict[str, Any]] = None

class TableRow(BaseModel):
    device_id: str
    timestamp: str
    kwh: float
    location: Optional[str] = None

# ----------------------------
# Blob helpers
# ----------------------------
def _list_latest_blob_names() -> List[str]:
    # Lists: latest/device-E-001.json etc.
    names = []
    for b in _container.list_blobs(name_starts_with=LATEST_PREFIX):
        if b.name.endswith(".json"):
            names.append(b.name)
    return names

def _download_json(blob_name: str) -> dict:
    blob = _container.get_blob_client(blob_name)
    raw = blob.download_blob().readall()
    return json.loads(raw)

def _load_all_latest_docs_cached() -> List[EnergySnapshot]:
    now = time.time()
    if _cache["latest_docs"] is not None and (now - _cache["ts"]) < _CACHE_TTL_SECONDS:
        return _cache["latest_docs"]

    blob_names = _list_latest_blob_names()
    docs: List[EnergySnapshot] = []

    for name in blob_names:
        try:
            obj = _download_json(name)
            docs.append(EnergySnapshot(**obj))
        except Exception as e:
            logger.warning("Failed parsing blob %s: %r", name, e)

    _cache["ts"] = now
    _cache["latest_docs"] = docs
    logger.info("Loaded %d latest snapshots from blob", len(docs))
    return docs

def _authorized_snapshots(claims: dict) -> List[EnergySnapshot]:
    role, device_id = _resolve_role_and_device(claims)
    docs = _load_all_latest_docs_cached()

    if role == "admin":
        return docs

    if not device_id:
        raise HTTPException(status_code=403, detail="No device_id claim")

    return [d for d in docs if d.device_id == device_id]

def _flatten_rows(docs: List[EnergySnapshot]) -> List[TableRow]:
    rows: List[TableRow] = []
    for doc in docs:
        for rec in doc.records:
            rows.append(
                TableRow(
                    device_id=doc.device_id,
                    timestamp=rec.timestamp,
                    kwh=rec.kwh,
                    location=rec.location,
                )
            )
    # Sort by timestamp descending (string timestamps often sortable; but keep robust by leaving as string)
    rows.sort(key=lambda r: r.timestamp, reverse=True)
    return rows

# ----------------------------
# Request logging middleware
# ----------------------------
@app.middleware("http")
async def request_logger(request: Request, call_next):
    start = time.time()
    response = await call_next(request)
    ms = int((time.time() - start) * 1000)
    logger.info("REQ %s %s -> %s (%dms)", request.method, request.url.path, response.status_code, ms)
    return response

# ----------------------------
# Routes
# ----------------------------
@app.get("/api/health")
def health():
    return {"status": "ok"}

@app.get("/api/profile")
def profile(claims: dict = Depends(require_auth)):
    role, device_id = _resolve_role_and_device(claims)
    out = {
        "role": role,
        "sub": claims.get("sub"),
        "username": claims.get("cognito:username") or claims.get("email"),
        "groups": claims.get("cognito:groups") or [],
    }
    if role != "admin":
        out["device_id"] = device_id
    return out

@app.get("/api/snapshots", response_model=List[EnergySnapshot])
def snapshots(claims: dict = Depends(require_auth)):
    # For debugging/demo: returns the ETL “latest” documents (filtered by RBAC)
    return _authorized_snapshots(claims)

@app.get("/api/data", response_model=List[TableRow])
def data_rows(claims: dict = Depends(require_auth)):
    docs = _authorized_snapshots(claims)
    return _flatten_rows(docs)

@app.get("/api/data/latest", response_model=TableRow)
def data_latest(claims: dict = Depends(require_auth)):
    docs = _authorized_snapshots(claims)
    rows = _flatten_rows(docs)
    if not rows:
        raise HTTPException(status_code=404, detail="No data")
    return rows[0]
