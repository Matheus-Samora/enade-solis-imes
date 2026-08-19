import os
import sys
import time
from flask import Flask, render_template, request, jsonify, send_from_directory
from enade import (
    SolisAPI,
    remover_acentos,
    processar_alunos_turma,
    sanitizar_nome_arquivo
)

# Garante UTF-8 no terminal Windows
if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

app = Flask(__name__, template_folder="templates")
api_solis = SolisAPI()

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/diagnostico")
def diagnostico():
    import requests
    diagnostico_info = {}
    
    user = os.getenv("SOLIS_WEB_USER", "12638612618")
    password = os.getenv("SOLIS_WEB_PASSWORD", "samora34892")
    base_url = os.getenv("SOLIS_API_URL", "https://academico.faculdadeimes.org.br").rstrip("/")
    report_id = os.getenv("SOLIS_REPORT_ID", "7720260819135105")

    diagnostico_info["user_configurado"] = user
    diagnostico_info["url_configurada"] = base_url
    diagnostico_info["report_id_configurado"] = report_id

    # 1. Teste de Autenticação na API do SolisGE
    try:
        auth_url = f"{base_url}/api/autenticar"
        resp_auth = requests.post(auth_url, data={"user": user, "password": password}, timeout=15)
        diagnostico_info["auth_status_code"] = resp_auth.status_code
        diagnostico_info["auth_response_preview"] = resp_auth.text[:300]
        
        if resp_auth.status_code == 200:
            try:
                token = resp_auth.json()
                diagnostico_info["token_obteve"] = True
                
                # 2. Teste de Chamada ao Relatório Genérico
                rep_url = f"{base_url}/api/basico/relatorio-generico/gerar/{report_id}"
                headers = {"X-Token": token, "Content-Type": "application/json"}
                resp_rep = requests.get(rep_url, headers=headers, json={"par": {}}, timeout=180)
                diagnostico_info["report_status_code"] = resp_rep.status_code
                if resp_rep.status_code == 200:
                    data_rep = resp_rep.json()
                    diagnostico_info["report_qtd_registros"] = len(data_rep) if isinstance(data_rep, list) else "Não é lista"
                else:
                    diagnostico_info["report_response_preview"] = resp_rep.text[:300]
            except Exception as e_json:
                diagnostico_info["token_obteve"] = False
                diagnostico_info["erro_json_auth"] = str(e_json)
        else:
            diagnostico_info["token_obteve"] = False
    except Exception as e:
        diagnostico_info["erro_conexao"] = str(e)

    return jsonify(diagnostico_info)

@app.route("/api/buscar-turmas", methods=["POST"])
def buscar_turmas():
    try:
        data = request.get_json() or {}
        termo = data.get("termo", "").strip()

        if not termo:
            return jsonify({"sucesso": False, "erro": "Termo de busca não informado."})

        # Re-inicializa credenciais dinamicamente com fallbacks diretos do SolisGE
        api_solis.user = os.getenv("SOLIS_WEB_USER", "12638612618")
        api_solis.password = os.getenv("SOLIS_WEB_PASSWORD", "samora34892")
        api_solis.base_url = os.getenv("SOLIS_API_URL", "https://academico.faculdadeimes.org.br").rstrip("/")

        if not api_solis.user or not api_solis.password:
            return jsonify({
                "sucesso": False,
                "erro": "⚠️ Credenciais do SolisGE não encontradas. Por favor, cadastre as variáveis SOLIS_WEB_USER e SOLIS_WEB_PASSWORD nas Environment Variables do Render."
            })

        alunos_todos = api_solis.carregar_base_geral_alunos()
        if not alunos_todos:
            msg_detalhada = api_solis.ultimo_erro or "Não foi possível obter dados do SolisGE. Verifique se o usuário/senha no Render estão corretos."
            return jsonify({
                "sucesso": False,
                "erro": f"⚠️ {msg_detalhada}"
            })

        termo_upper = remover_acentos(termo)
        turmas_encontradas = {}

        for aluno in alunos_todos:
            nome_turma = aluno.get("Turma") or aluno.get("codigo_turma") or ""
            situacao = remover_acentos(aluno.get("Situação") or aluno.get("situacao") or "")

            # REGRA DE NEGÓCIO: Ignora alunos vestibulandos
            if "VESTIBULANDO" in situacao:
                continue

            if nome_turma and termo_upper in remover_acentos(nome_turma):
                if nome_turma not in turmas_encontradas:
                    turmas_encontradas[nome_turma] = []
                turmas_encontradas[nome_turma].append(aluno)

        lista_turmas = sorted(turmas_encontradas.keys())
        turmas_formatadas = []

        for t_nome in lista_turmas:
            turmas_formatadas.append({
                "nome": t_nome,
                "qtd": len(turmas_encontradas[t_nome])
            })

        return jsonify({
            "sucesso": True,
            "turmas": turmas_formatadas,
            "turmas_map": turmas_encontradas
        })
    except Exception as e:
        return jsonify({"sucesso": False, "erro": f"Erro interno no servidor: {str(e)}"})

@app.route("/api/processar-turma", methods=["POST"])
def processar_turma():
    try:
        data = request.get_json() or {}
        turmas_selecionadas = data.get("turmas", [])
        turmas_map = data.get("turmas_map", {})

        if not turmas_selecionadas:
            return jsonify({"sucesso": False, "erro": "Nenhuma turma selecionada."})

        resultados = []

        for t_nome in turmas_selecionadas:
            alunos = turmas_map.get(t_nome, [])
            if alunos:
                zips_antes = set(f for f in os.listdir(".") if f.startswith("ENADE2611101_") and f.endswith(".zip"))

                processar_alunos_turma(t_nome, alunos, api_solis)
                sufixo = sanitizar_nome_arquivo(t_nome)
                
                nome_p1 = f"Planilha1_Entrada_Turma_{sufixo}.xlsx"
                nome_p2 = f"Planilha2_Convertida_ENADE_Turma_{sufixo}.xlsx"

                zips_depois = set(f for f in os.listdir(".") if f.startswith("ENADE2611101_") and f.endswith(".zip"))
                novos_zips = zips_depois - zips_antes
                
                nome_zip = list(novos_zips)[0] if novos_zips else ""
                if not nome_zip:
                    zips_ordenados = sorted(list(zips_depois), key=lambda x: os.path.getmtime(x), reverse=True)
                    nome_zip = zips_ordenados[0] if zips_ordenados else ""

                preview_alunos = []
                if os.path.exists(nome_p1):
                    try:
                        import pandas as pd
                        df = pd.read_excel(nome_p1)
                        df = df.fillna("")
                        preview_alunos = df.head(10).to_dict("records")
                    except Exception:
                        pass

                resultados.append({
                    "turma": t_nome,
                    "qtd_alunos": len(alunos),
                    "p1_download": f"/download/{nome_p1}",
                    "p2_download": f"/download/{nome_p2}",
                    "zip_download": f"/download/{nome_zip}" if nome_zip else "#",
                    "nome_zip": nome_zip,
                    "preview": preview_alunos
                })

        return jsonify({
            "sucesso": True,
            "resultados": resultados
        })
    except Exception as e:
        return jsonify({"sucesso": False, "erro": f"Erro interno ao processar turma: {str(e)}"})

@app.route("/download/<filename>")
def download_file(filename):
    return send_from_directory(".", filename, as_attachment=True)

if __name__ == "__main__":
    print("\n" + "=" * 65)
    print("🚀 SERVIDOR WEB DO ENADE 2026 INICIADO COM SUCESSO!")
    print("🌐 Acesse no seu navegador: http://localhost:5000")
    print("=" * 65 + "\n")
    app.run(host="0.0.0.0", port=5000, debug=False)
