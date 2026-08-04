"""
main.py — TipMike API
Entry point. Roda com:
    uvicorn main:app --host 0.0.0.0 --port 8000 --reload

Fase 3 (auth): TODAS as rotas de negócio exigem Bearer token válido
(access de usuário ou token de serviço). Somente /auth/* e a raiz "/"
permanecem públicas.
"""

from contextlib import asynccontextmanager
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from database import init_pool, close_pool
from security import get_current_user
from routers import sistema, ticks, h2h, eventos, bots, apostas, stats, torneios, backtest, historico, telegram, backtest_upload, auth, admin, h2h_sync, mikedb


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Inicializa o pool ao subir
    print("Conectando MikeDB...")
    await init_pool()
    print("MikeDB conectado")
    yield
    # Fecha o pool ao desligar
    await close_pool()
    print("MikeDB desconectado")


app = FastAPI(
    title="TipMike API",
    description="API de dados e gestão de bots de apostas esportivas",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS — permite chamadas do Vercel e localhost
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://tipmike.vercel.app",
        "https://tipmike.com.br",
        "https://www.tipmike.com.br",
        "http://localhost:5173",
        "http://localhost:3000",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Registra routers ─────────────────────────────────────────────
# Fase 3: dependencies=PROTEGIDO exige Bearer válido em TODAS as
# rotas do router. Usuário desativado ou token expirado → 401/403.
PROTEGIDO = [Depends(get_current_user)]

# Público: login/refresh/registro (o registro em si é controlado por
# env var + role admin dentro do próprio router).
app.include_router(auth.router)
app.include_router(admin.router, dependencies=PROTEGIDO)

# Protegido: todo o resto.
app.include_router(sistema.router, dependencies=PROTEGIDO)
app.include_router(ticks.router, dependencies=PROTEGIDO)
app.include_router(h2h.router, dependencies=PROTEGIDO)
app.include_router(eventos.router, dependencies=PROTEGIDO)
app.include_router(bots.router, dependencies=PROTEGIDO)
app.include_router(apostas.router, dependencies=PROTEGIDO)
app.include_router(stats.router, dependencies=PROTEGIDO)
app.include_router(torneios.router, dependencies=PROTEGIDO)
app.include_router(backtest.router, dependencies=PROTEGIDO)
app.include_router(historico.router, dependencies=PROTEGIDO)
app.include_router(telegram.router, dependencies=PROTEGIDO)
app.include_router(backtest_upload.router, dependencies=PROTEGIDO)
app.include_router(h2h_sync.router, dependencies=PROTEGIDO)
app.include_router(mikedb.router, dependencies=PROTEGIDO)


@app.get("/")
async def root():
    return {
        "nome": "TipMike API",
        "versao": "1.0.0",
        "docs": "/docs",
        "status": "online",
    }
