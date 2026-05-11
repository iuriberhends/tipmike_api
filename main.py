"""
main.py — TipMike API
Entry point. Roda com:
    uvicorn main:app --host 0.0.0.0 --port 8000 --reload
"""

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from database import init_pool, close_pool
from routers import sistema, ticks, h2h, eventos, bots, apostas, stats, torneios, backtest, historico, telegram 


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
        "http://localhost:5173",
        "http://localhost:3000",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Registra routers
app.include_router(sistema.router)
app.include_router(ticks.router)
app.include_router(h2h.router)
app.include_router(eventos.router)
app.include_router(bots.router)
app.include_router(apostas.router)
app.include_router(stats.router)
app.include_router(torneios.router)
app.include_router(backtest.router)
app.include_router(historico.router)
app.include_router(telegram.router)


@app.get("/")
async def root():
    return {
        "nome": "TipMike API",
        "versao": "1.0.0",
        "docs": "/docs",
        "status": "online",
    }
