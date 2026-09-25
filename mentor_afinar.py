# -*- coding: utf-8 -*-
"""
mentor_afinar.py v2 — "cada aula tem as suas questões", agora por SENTIDO, sem API paga.

Como decide a que aula uma questão pertence:
  1. Embeddings locais (intfloat/multilingual-e5-small, roda na CPU): questão e trechos da aula viram
     vetores de significado; "sublinhado vermelho" fica perto de "corretor ortográfico" sem palavra igual.
  2. Trechos da aula = título + tópicos do resumo de bolso e do mapa mental + as QUESTÕES DE FIXAÇÃO do
     Gran (escritas pra aquela aula: são o gabarito do vínculo) + janelas da transcrição quando faltar resumo.
     A semelhança questão↔aula é a maior semelhança com qualquer trecho; trecho de fixação ganha um bônus.
  3. Margem: se duas aulas empatam (diferença < 0,015), a questão vale pras duas.
  4. Duas passadas: dentro do assunto (qual parte: Writer II ou V) e entre assuntos da matéria
     (Dolo ou Furto); aulas de exercícios/resolução não "possuem" questão.
  5. Feedback do aluno (tabela mentor.vinculo_feedback: "não é desta aula") manda em tudo.

Grava em mentor.questao_aula: sim, melhor (dentro do assunto), melhor_mat (na matéria).
Embeddings ficam em mentor.embedding (cache): a 2ª rodada só calcula o que é novo.
Sem o modelo instalado, cai pro TF-IDF (v1) e avisa.

USO (raiz do tipmike_api, no .venv):
    pip install sentence-transformers            (uma vez; ~300 MB com o torch de CPU)
    python mentor_afinar.py --migrar             (tabelas/colunas; idempotente)
    python mentor_afinar.py --tudo               (12 matérias; ~30-40 min na 1ª vez, minutos depois)
    python mentor_afinar.py --tudo --materia 6   (só uma matéria)
    python mentor_afinar.py --simular            (calcula e mostra, não grava)
"""
import argparse
import math
import os
import re
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
try:
    from dotenv import load_dotenv
    load_dotenv(RAIZ / ".env")
except Exception:
    pass
DSN_PADRAO = (os.environ.get("MIKEDB_DSN") or os.environ.get("MENTOR_DSN") or os.environ.get("DATABASE_URL")
              or "postgresql://postgres:mikedb0702@localhost:5432/mikedb")
MODELO = os.environ.get("MENTOR_EMBED_MODELO", "intfloat/multilingual-e5-small")
MARGEM_Z = 0.25         # empate: diferença de escore combinado (z) menor que isso vale pras duas
TAM_QUESTAO = 1200      # caracteres da questão que entram no embedding (menor = mais rápido)
MIN_PALAVRA = 0.06      # evidência mínima por palavra pra ter dona…
MIN_Z = 2.5             # …ou destaque forte no sentido
BONUS_FIXACAO = 0.02
MAX_CHUNKS_AULA = 60

MIGRACAO = """
ALTER TABLE mentor.questao_aula ADD COLUMN IF NOT EXISTS sim NUMERIC(5,3);
ALTER TABLE mentor.questao_aula ADD COLUMN IF NOT EXISTS melhor BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE mentor.questao_aula ADD COLUMN IF NOT EXISTS melhor_mat BOOLEAN NOT NULL DEFAULT FALSE;
CREATE INDEX IF NOT EXISTS ix_qa_melhor ON mentor.questao_aula(aula_id) WHERE melhor;
CREATE INDEX IF NOT EXISTS ix_qa_melhor_mat ON mentor.questao_aula(aula_id) WHERE melhor_mat;
CREATE TABLE IF NOT EXISTS mentor.embedding (
    tipo TEXT NOT NULL, ref BIGINT NOT NULL, chunk INT NOT NULL DEFAULT 0, modelo TEXT NOT NULL,
    texto_hash TEXT NOT NULL, vetor BYTEA NOT NULL, criado_em TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tipo, ref, chunk, modelo));
CREATE TABLE IF NOT EXISTS mentor.vinculo_feedback (
    questao_id BIGINT NOT NULL, aula_id BIGINT NOT NULL, ok BOOLEAN NOT NULL, criado_em TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (questao_id, aula_id));
"""

TOKEN = re.compile(r"[a-z0-9]{3,}")
STOP = set("""que nao com para uma por dos das mais como seu sua sao esta este essa esse sobre entre pode ser foi tem
seus suas aos nas nos qual quais quando onde apenas assim mesmo tambem ainda sendo pela pelo pelos pelas art arts
questao alternativa correta incorreta assinale considere acerca segundo conforme texto acima abaixo item itens julgue
afirmativa afirmativas verdadeira falsa verdadeiro falso caso casos gente aqui entao vamos aula professor pessoal""".split())


def log(m):
    print(m, flush=True)


def sem_acento(t):
    t = unicodedata.normalize("NFKD", str(t or ""))
    return "".join(c for c in t if not unicodedata.combining(c)).lower()


def limpar(t, n=900):
    t = re.sub(r"<[^>]+>", " ", str(t or ""))
    t = re.sub(r"\s+", " ", t).strip()
    return t[:n]


def hash_txt(t):
    import hashlib
    return hashlib.md5(t.encode("utf-8", "ignore")).hexdigest()


# ---------------------------------------------------------------- embeddings (com fallback)
class Embedder:
    def __init__(self):
        self.modelo = None
        self.nome = MODELO
        try:
            from sentence_transformers import SentenceTransformer
            self.modelo = SentenceTransformer(MODELO)
            log(f"  modelo de embeddings: {MODELO} (dim {self.modelo.get_sentence_embedding_dimension()})")
        except Exception as e:
            log(f"  [!] sentence-transformers indisponível ({type(e).__name__}): usando TF-IDF (v1). pip install sentence-transformers")
            self.nome = "tfidf"

    def codificar(self, textos, tipo):
        """textos -> lista de vetores numpy normalizados. tipo: 'query' (questão) ou 'passage' (aula)."""
        import numpy as np
        if self.modelo is None:
            return None
        pref = "query: " if tipo == "query" else "passage: "
        v = self.modelo.encode([pref + t for t in textos], batch_size=64, normalize_embeddings=True, show_progress_bar=False)
        return np.asarray(v, dtype=np.float32)


def vet_para_bytes(v):
    import numpy as np
    return np.asarray(v, dtype=np.float16).tobytes()


def bytes_para_vet(b):
    import numpy as np
    return np.frombuffer(b, dtype=np.float16).astype(np.float32)


def embed_cache(cur, emb, tipo, itens):
    """itens: lista de (ref, chunk, texto). Devolve dict (ref,chunk) -> vetor, usando o cache no banco."""
    import psycopg2.extras as ex
    out = {}
    if not itens:
        return out
    refs = list({r for r, _, _ in itens})
    cur.execute("SELECT ref, chunk, texto_hash, vetor FROM mentor.embedding WHERE tipo=%s AND modelo=%s AND ref = ANY(%s)", (tipo, emb.nome, refs))
    cache = {(r, c): (h, v) for r, c, h, v in cur.fetchall()}
    faltam = []
    for ref, chunk, texto in itens:
        h = hash_txt(texto)
        c = cache.get((ref, chunk))
        if c and c[0] == h:
            out[(ref, chunk)] = bytes_para_vet(c[1])
        else:
            faltam.append((ref, chunk, texto, h))
    for i in range(0, len(faltam), 512):
        lote = faltam[i:i + 512]
        vets = emb.codificar([t for _, _, t, _ in lote], "query" if tipo == "questao" else "passage")
        linhas = []
        for (ref, chunk, texto, h), v in zip(lote, vets):
            out[(ref, chunk)] = v
            linhas.append((tipo, ref, chunk, emb.nome, h, psycopg2_bin(vet_para_bytes(v))))
        ex.execute_values(cur, """INSERT INTO mentor.embedding (tipo, ref, chunk, modelo, texto_hash, vetor) VALUES %s
                                  ON CONFLICT (tipo, ref, chunk, modelo) DO UPDATE SET texto_hash=EXCLUDED.texto_hash, vetor=EXCLUDED.vetor, criado_em=now()""", linhas, page_size=500)
    return out


def psycopg2_bin(b):
    import psycopg2
    return psycopg2.Binary(b)


# ---------------------------------------------------------------- trechos da aula
def trechos_da_aula(a):
    """a: dict com titulo, resumo, resumo_bolso, mapa_mental, questoes_fixacao, transcricao. -> [(texto, eh_fixacao)]"""
    out = [(limpar(a["titulo"], 200), False)]
    fix = a.get("questoes_fixacao")
    if isinstance(fix, str):
        try:
            import json
            fix = json.loads(fix)
        except Exception:
            fix = []
    for q in (fix or [])[:10]:
        if not isinstance(q, dict):
            continue
        alts = q.get("alternatives") or q.get("alternativas") or []
        certa = next((x for x in alts if isinstance(x, dict) and (x.get("correct") or x.get("correta"))), {})
        txt = f"{q.get('question') or q.get('enunciado') or ''} {certa.get('text') or certa.get('texto') or ''} {certa.get('explanation') or certa.get('explicacao') or ''}"
        if len(txt) > 30:
            out.append((limpar(txt, 700), True))
    topicos = []
    for campo in ("resumo_bolso", "mapa_mental", "resumo"):
        t = a.get(campo) or ""
        for linha in re.split(r"\n+", t):
            linha = re.sub(r"^[\s#*\-•>\d.)]+", "", linha).strip()
            if 25 <= len(linha) <= 600:
                topicos.append(linha)
    for t in topicos[:MAX_CHUNKS_AULA - len(out)]:
        out.append((limpar(t, 600), False))
    if len(out) < 8:
        tr = limpar(a.get("transcricao") or "", 40000)
        for i in range(0, min(len(tr), 30000), 700):
            out.append((tr[i:i + 700], False))
            if len(out) >= MAX_CHUNKS_AULA:
                break
    return out[:MAX_CHUNKS_AULA]


# ---------------------------------------------------------------- TF-IDF (fallback)
def tokens(t):
    return [w for w in TOKEN.findall(sem_acento(t)) if w not in STOP]


def tfidf_vetores(docs_tokens):
    df = Counter()
    for d in docs_tokens:
        df.update(set(d))
    N = len(docs_tokens)
    idf = {w: math.log((N + 1) / (n + 1)) + 1 for w, n in df.items()}

    def vet(toks):
        c = Counter(toks)
        v = {w: (1 + math.log(n)) * idf.get(w, 0.0) for w, n in c.items() if w in idf}
        norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
        return {w: x / norm for w, x in v.items()}
    return idf, vet


def cos_esparso(a, b):
    if len(a) > len(b):
        a, b = b, a
    return sum(x * b.get(w, 0.0) for w, x in a.items())


# ---------------------------------------------------------------- núcleo
def afinar(dsn, simular, materia=None):
    import numpy as np
    import psycopg2
    import psycopg2.extras as ex
    emb = Embedder()
    conn = psycopg2.connect(dsn)
    conn.autocommit = False
    cur = conn.cursor()
    try:
        cur.execute("SELECT 1 FROM information_schema.tables WHERE table_schema='mentor' AND table_name='embedding'")
        if not cur.fetchone():
            raise SystemExit("rode --migrar antes")
        cur.execute("SELECT id, nome FROM mentor.materia WHERE (%s::int IS NULL OR id = %s) ORDER BY id", (materia, materia))
        materias = cur.fetchall()
        cur.execute("SELECT questao_id, aula_id, ok FROM mentor.vinculo_feedback")
        feedback = {(q, a): ok for q, a, ok in cur.fetchall()}
        t0 = time.time()
        for mid, mnome in materias:
            # ---- aulas e trechos
            cur.execute("""SELECT a.id, a.assunto_id, a.ordem, a.titulo, a.resumo, a.resumo_bolso, a.mapa_mental, a.questoes_fixacao::text, a.transcricao, s.nome
                           FROM mentor.aula a JOIN mentor.assunto s ON s.id=a.assunto_id WHERE s.materia_id=%s AND a.tipo <> 'fora' ORDER BY s.id, a.ordem""", (mid,))
            aulas = [dict(zip(("id", "assunto_id", "ordem", "titulo", "resumo", "resumo_bolso", "mapa_mental", "questoes_fixacao", "transcricao", "assunto"), r)) for r in cur.fetchall()]
            if not aulas:
                continue
            cur.execute("SELECT s.id, s.nome, mo.tipo FROM mentor.assunto s JOIN mentor.modulo mo ON mo.id=s.modulo_id WHERE s.materia_id=%s", (mid,))
            linhas_ass = cur.fetchall()
            nome_ass = {sid: n for sid, n, _ in linhas_ass}
            # exercícios/resolução e o Treinamento Intensivo (revisão) não "possuem" questão: são donos só se não houver outro
            exercicio = {sid for sid, n, tipo in linhas_ass if tipo == 'revisao' or re.match(r"^\s*(Exerc[ií]cios|Resolu[çc][ãa]o)", n or "", re.I)}
            itens = []
            fix_flag = {}
            for a in aulas:
                for k, (txt, fx) in enumerate(trechos_da_aula(a)):
                    itens.append((a["id"], k, txt))
                    fix_flag[(a["id"], k)] = fx
            # ---- questões da matéria (ligadas a alguma aula)
            cur.execute("""SELECT DISTINCT q.id, q.enunciado, q.alternativas::text FROM mentor.questao_aula qa JOIN mentor.assunto s ON s.id=qa.assunto_id
                           JOIN mentor.questao q ON q.id=qa.questao_id WHERE s.materia_id=%s AND q.usavel""", (mid,))
            qs = cur.fetchall()
            cur.execute("""SELECT qa.questao_id, qa.aula_id, qa.assunto_id FROM mentor.questao_aula qa JOIN mentor.assunto s ON s.id=qa.assunto_id WHERE s.materia_id=%s""", (mid,))
            ligados = defaultdict(set)
            for qid, aid, sid in cur.fetchall():
                ligados[qid].add(aid)
            log(f"  [{mid:2d}] {mnome[:30]:30s} aulas {len(aulas):3d} · trechos {len(itens):5d} · questões {len(qs):6d} · codificando…")
            # TF-IDF (palavra) sempre: pega termo específico (lpstat, justificado, Writer) que o embedding dilui
            toks_a = {a["id"]: tokens(" ".join(t for t, _ in trechos_da_aula(a))) for a in aulas}
            idf, vet = tfidf_vetores(list(toks_a.values()))
            vets_a = {aid: vet(t) for aid, t in toks_a.items()}
            vets_q = {qid: vet(tokens(f"{e} {al}")) for qid, e, al in qs}
            mats = {}
            vq = {}
            if emb.modelo is not None:
                va = embed_cache(cur, emb, "aula", itens)
                vq = embed_cache(cur, emb, "questao", [(qid, 0, limpar(f"{e} {al}", TAM_QUESTAO)) for qid, e, al in qs])
                for a in aulas:
                    ks = sorted(k for (ref, k) in va if ref == a["id"])
                    if ks:
                        M = np.stack([va[(a["id"], k)] for k in ks])
                        bonus = np.array([BONUS_FIXACAO if fix_flag.get((a["id"], k)) else 0.0 for k in ks], dtype=np.float32)
                        mats[a["id"]] = (M, bonus)

            def sim_emb(qid, aid):
                m = mats.get(aid)
                v = vq.get((qid, 0))
                if m is None or v is None:
                    return None
                return float((m[0] @ v + m[1]).max())

            def sim_tfidf(qid, aid):
                return cos_esparso(vets_q[qid], vets_a[aid])

            def z(d):
                vals = list(d.values())
                if len(vals) < 2:
                    return {k: 0.0 for k in d}
                mu = sum(vals) / len(vals)
                sd = (sum((x - mu) ** 2 for x in vals) / len(vals)) ** 0.5 or 1e-6
                return {k: (x - mu) / sd for k, x in d.items()}

            def escores(qid, aids):
                """escore combinado por aula = z(palavra) + z(sentido), calculado sobre as aulas candidatas da questão."""
                st = z({aid: sim_tfidf(qid, aid) for aid in aids})
                se_raw = {aid: sim_emb(qid, aid) for aid in aids}
                se = z({k: v for k, v in se_raw.items() if v is not None}) if any(v is not None for v in se_raw.values()) else {}
                return {aid: st.get(aid, 0.0) + se.get(aid, 0.0) for aid in aids}

            # ---- decisão por questão: candidatas = aulas dos assuntos ligados; escore z(palavra)+z(sentido)
            aula_por_id = {a["id"]: a for a in aulas}
            por_assunto = defaultdict(list)
            for a in aulas:
                por_assunto[a["assunto_id"]].append(a["id"])
            upd, novos = [], []
            dist_ass = Counter()
            dist_aula = Counter()
            for qid, _, _ in qs:
                # candidatas: TODAS as aulas de conteúdo da matéria (a etiqueta do Gran liga Linux a Windows e Excel a Writer;
                # a disputa tem que incluir a aula certa mesmo que a etiqueta não a tenha ligado)
                ass_lig = {sid for sid in por_assunto if sid not in exercicio} or set(por_assunto)
                cands = [aid for sid in ass_lig for aid in por_assunto[sid]]
                if not cands:
                    continue
                esc = escores(qid, cands)
                top_geral = max(esc.values())
                # assunto dono: o do melhor escore, ignorando exercícios/revisão quando houver alternativa
                melhor_aula_ass = {sid: max((esc[a], a) for a in por_assunto[sid]) for sid in ass_lig if por_assunto[sid]}
                cand_ass = {sid: v for sid, v in melhor_aula_ass.items() if sid not in exercicio} or melhor_aula_ass
                top_ass = max(v[0] for v in cand_ass.values())
                donos = {sid for sid, v in cand_ass.items() if v[0] >= top_ass - MARGEM_Z}
                # evidência mínima: se nenhuma aula tem palavra em comum com a questão nem se destaca no sentido,
                # a questão fica SEM dona (o Gran não ensina isso; ela vale só na aba Questões)
                melhor_aid = max(esc, key=esc.get)
                if sim_tfidf(qid, melhor_aid) < MIN_PALAVRA and esc[melhor_aid] < MIN_Z:
                    donos = set()
                for sid in ass_lig:
                    aids = por_assunto[sid]
                    top_local = max(esc[a] for a in aids)
                    for aid in aids:
                        sc = esc[aid]
                        m = sc >= top_local - MARGEM_Z
                        mm = m and sid in donos
                        fb = feedback.get((qid, aid))
                        if fb is not None:
                            m, mm = fb, fb
                        sim_val = sim_emb(qid, aid)
                        sim_val = round(sim_val, 3) if sim_val is not None else round(sim_tfidf(qid, aid), 3)
                        if aid in ligados[qid]:
                            upd.append((sim_val, m, mm, qid, aid))
                        elif mm and sc >= top_geral:
                            novos.append((qid, aid, sid, sim_val, mm))
                        if mm:
                            dist_aula[aid] += 1
                for sid in donos:
                    dist_ass[nome_ass.get(sid, "?")] += 1
            if not simular:
                ex.execute_batch(cur, "UPDATE mentor.questao_aula SET sim=%s, melhor=%s, melhor_mat=%s WHERE questao_id=%s AND aula_id=%s", upd, page_size=2000)
                if novos:
                    ex.execute_values(cur, """INSERT INTO mentor.questao_aula (questao_id, aula_id, assunto_id, via, forca, sim, melhor, melhor_mat) VALUES %s ON CONFLICT DO NOTHING""",
                                      [(q, a, s, "semelhanca", 0.5, sm, True, mm) for q, a, s, sm, mm in novos], page_size=2000)
                conn.commit()
            top = ", ".join(f"{n[:22]}:{c}" for n, c in dist_ass.most_common(6))
            log(f"       donos (top 6): {top}")
            for sid, aids in por_assunto.items():
                if len(aids) >= 2 and sum(dist_aula.get(a, 0) for a in aids) >= 20:
                    log(f"       {nome_ass.get(sid, '?')[:34]:34s} " + " ".join(f"{aula_por_id[a]['ordem']}:{dist_aula.get(a, 0)}" for a in aids))
            log(f"       pares {len(upd)} · vínculos novos {len(novos)} · {time.time() - t0:.0f}s")
        if simular:
            conn.rollback()
            log("  --simular: rollback, nada gravado (os embeddings também não)")
        else:
            conn.commit()
            log("  commit")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default=DSN_PADRAO)
    ap.add_argument("--migrar", action="store_true")
    ap.add_argument("--simular", action="store_true")
    ap.add_argument("--tudo", action="store_true")
    ap.add_argument("--materia", type=int, default=None)
    a = ap.parse_args()
    log(f"  DSN: ...@{a.dsn.split('@')[-1]}")
    if a.migrar:
        import psycopg2
        c = psycopg2.connect(a.dsn)
        with c:
            with c.cursor() as cur:
                cur.execute(MIGRACAO)
        c.close()
        log("  tabelas e colunas prontas (questao_aula.sim/melhor/melhor_mat, embedding, vinculo_feedback)")
        if not (a.tudo or a.simular):
            return
    if not (a.tudo or a.simular):
        ap.error("use --migrar, --simular ou --tudo")
    afinar(a.dsn, a.simular, a.materia)


if __name__ == "__main__":
    main()
