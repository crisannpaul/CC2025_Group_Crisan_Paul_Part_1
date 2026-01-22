from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, field_validator
from typing import List, Optional, Tuple
import json
from pathlib import Path
from datetime import datetime
import os
import time
import logging

import jwt
from jwt import PyJWKClient

# ----------------------------
# App + logging
# ----------------------------
app = FastAPI(title="Sensor API", version="0.3.0")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sensor-api")

# ----------------------------
# CORS
# ----------------------------
origins = [
    "http://localhost:8080",
    "http://127.0.0.1:8080",
    "http://localhost:5500",
    "http://127.0.0.1:5500",
    "https://kind-dune-0fa1d2103.3.azurestaticapps.net",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=False,  # Bearer tokens; no cookies needed
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
    Cognito JWT verification (ID token OR access token) without PyJWT audience auto-check.
    We do aud/client_id checks manually to avoid InvalidAudienceError surprises.
    """
    try:
        # Log unverified claims for debugging (no signature validation yet)
        unverified = jwt.decode(token, options={"verify_signature": False})
        logger.info(
            "UNVERIFIED token_use=%s iss=%s aud=%s client_id=%s",
            unverified.get("token_use"),
            unverified.get("iss"),
            unverified.get("aud"),
            unverified.get("client_id"),
        )

        signing_key = _jwk_client.get_signing_key_from_jwt(token).key

        # Verify signature + issuer + exp/iat. Do NOT let PyJWT validate aud automatically.
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

        # Bind token to the correct app client depending on token type
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

    # Basic auth logging (never log the token)
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
# Data model + loader
# ----------------------------
DATA_PATH = Path(__file__).parent / "data.json"

class SensorRow(BaseModel):
    timestamp: str
    device_id: str
    temperature: float
    humidity: float
    voltage: float

    @field_validator("timestamp")
    @classmethod
    def validate_ts(cls, v: str) -> str:
        datetime.fromisoformat(v.replace("Z", "+00:00"))
        return v

def load_data() -> List[SensorRow]:
    if not DATA_PATH.exists():
        return []
    with open(DATA_PATH, "r", encoding="utf-8") as f:
        raw = json.load(f)
    return [SensorRow(**row) for row in raw]

# ----------------------------
# Routes + request logging
# ----------------------------
@app.middleware("http")
async def request_logger(request: Request, call_next):
    start = time.time()
    response = await call_next(request)
    ms = int((time.time() - start) * 1000)
    logger.info("REQ %s %s -> %s (%dms)", request.method, request.url.path, response.status_code, ms)
    return response

@app.get("/api/health")
def health():
    return {"status": "ok"}

@app.get("/api/profile")
def profile(request: Request, claims: dict = Depends(require_auth)):
    role, device_id = _resolve_role_and_device(claims)

    out = {
        "role": role,
        "sub": claims.get("sub"),
        "username": claims.get("cognito:username") or claims.get("email"),
        "groups": claims.get("cognito:groups") or [],
    }

    # For users, include device_id; for admins it's optional/usually None
    if role != "admin":
        out["device_id"] = device_id

    return out

@app.get("/api/data", response_model=List[SensorRow])
def get_data(request: Request, claims: dict = Depends(require_auth)):
    data = load_data()
    role, device_id = _resolve_role_and_device(claims)

    # Admin sees everything (even if device_id is None)
    if role == "admin":
        return data

    # User must have device_id claim and is restricted to it
    if not device_id:
        logger.warning("AUTH_FORBIDDEN missing device_id sub=%s", claims.get("sub"))
        raise HTTPException(status_code=403, detail="No device_id claim")

    return [r for r in data if r.device_id == device_id]

@app.get("/api/data/latest", response_model=SensorRow)
def get_latest(request: Request, claims: dict = Depends(require_auth)):
    data = load_data()
    role, device_id = _resolve_role_and_device(claims)

    # Admin: latest across all devices
    if role != "admin":
        # User: latest only for their device
        if not device_id:
            logger.warning("AUTH_FORBIDDEN missing device_id sub=%s", claims.get("sub"))
            raise HTTPException(status_code=403, detail="No device_id claim")
        data = [r for r in data if r.device_id == device_id]

    if not data:
        raise HTTPException(status_code=404, detail="No data")

    latest = max(data, key=lambda r: r.timestamp)
    return latest
