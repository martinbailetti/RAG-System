"""Modelos Pydantic para la API de consulta RAG."""

from pydantic import BaseModel, validator
from typing import Optional, List, Dict, Any


class ConsultaRequest(BaseModel):
    pregunta: str
    rutas: Optional[List[str]] = None  # lista de rutas a filtrar (opcionales)
    root_folder: Optional[str] = None  # restringir por carpeta raíz (opcional)
    query_paths: Optional[str] = None  # prefijos de ruta separados por coma (opcional)

    # Contexto conversacional opcional (para coherencia entre turnos)
    conversation_id: Optional[int] = None
    conversation: Optional[List[Dict[str, Any]]] = None

    class Config:
        extra = "ignore"


class ChatMessage(BaseModel):
    role: str
    content: str

    @validator("role", pre=True)
    def normalize_role(cls, v):
        role = str(v or "").strip().lower()
        if role in {"assistant", "ai", "bot"}:
            return "assistant"
        if role == "system":
            return "system"
        return "user"

    @validator("content", pre=True)
    def normalize_content(cls, v):
        return str(v or "").strip()


class ConsultaResponse(BaseModel):
    respuesta: str
    fuentes: list[str]
    usage: dict[str, int] | None = None
    found: bool = True
    greeting: bool = False
    needs_clarification: bool = False
    all_sources: bool = False
    debug_info: dict[str, Any] | None = None


class DocumentoInfo(BaseModel):
    ruta: str
    nombre: str
    carpeta: str
    chunks: int
    hash: Optional[str] = None
    mtime: Optional[float] = None
    size: Optional[int] = None


class DocumentosResponse(BaseModel):
    documentos: list[DocumentoInfo]
    total: int
    total_chunks: int


class EliminarDocumentoResponse(BaseModel):
    deleted: int
    ruta: str


class DocumentoDetalle(BaseModel):
    ruta: str
    nombre: str
    carpeta: str
    chunks: int
    hash: Optional[str] = None
    mtime: Optional[float] = None
    size: Optional[int] = None
    texto: str  # texto completo concatenado de todos los chunks


class IngestaRequest(BaseModel):
    carpeta: Optional[str] = None
    # Si None (default) o True, reinicia el servidor uvicorn tras la ingesta
    # SOLO si hubo cambios reales (procesados o eliminados). Pasar False
    # explícitamente para suprimir el reinicio.
    restart: Optional[bool] = None


class DocumentoPendiente(BaseModel):
    ruta: str
    nombre: str
    carpeta: str
    estado: str  # "nuevo" | "modificado" | "eliminado" | "oculto"
    hash: Optional[str] = None
    size: Optional[int] = None
    mtime: Optional[float] = None


class PendientesResponse(BaseModel):
    carpeta: str
    nuevos: list[DocumentoPendiente]
    modificados: list[DocumentoPendiente]
    eliminados: list[DocumentoPendiente]
    ocultos: list[DocumentoPendiente]
    total: int


class IngestaStatusResponse(BaseModel):
    running: bool


class TreeNode(BaseModel):
    name: str
    path: str
    type: str  # siempre "directory" en este contexto
    children: Optional[List["TreeNode"]] = None

    class Config:
        arbitrary_types_allowed = True


class FaqsResponse(BaseModel):
    texto: str
    ruta: str


class FaqsUpdateRequest(BaseModel):
    texto: str


TreeNode.update_forward_refs()
