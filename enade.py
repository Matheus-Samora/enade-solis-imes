import csv
import re
import os
import sys
import unicodedata
import zipfile
import json
import requests
from dotenv import load_dotenv

# Garante suporte a UTF-8 no terminal Windows
if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

# Carrega variáveis de ambiente do arquivo .env
load_dotenv()

# ==========================================
# CONFIGURAÇÕES DO NOVO LAYOUT ENADE 2026
# ==========================================
CO_PROJETO = "2611101"              # Código do projeto atualizado pelo Inep
TP_ORIGEM = "E"
CO_IES = "18637"
CO_TURNO_GRADUACAO = "3"
NU_PERCENTUAL_INTEGRALIZACAO = "80.0"  # Valor fixado exigido pelo Manual (Float 5.2)
IN_MUNICIPIO_POLO_EXTERIOR = "0"
ANO_CORRENTE = "2026"               # Ano base do Enade para validação de semestre

# Configurações de Nomenclatura
PREFIXO_NOME_ARQUIVO = "ENADE2611101_N99_BR_21062025"

# Relatório Genérico do Solis GE
ID_RELATORIO_GENERICO = os.getenv("SOLIS_REPORT_ID", "7720260819135105")  # Relatório ENADE com Polo do Contrato
HTTP_TIMEOUT_SOLIS = 600                     # Timeout estendido para 10 minutos (600s)

# Cabeçalho exato exigido pelo INEP (validado em 19/05/2026)
CABECHALHO_INEP = (
    "CO_PROJETO;TP_ORIGEM;CO_IES;CO_CURSO;NU_CPF;NU_ANO_FIM_ENSINO_MEDIO;"
    "CO_TURNO_GRADUACAO;NU_PERCENTUAL_INTEGRALIZACAO;NU_ANO_FORMATURA;"
    "NU_SEMESTRE_FORMATURA;NU_ANO_INICIO_GRADUACAO;IN_MUNICIPIO_POLO_EXTERIOR;"
    "CO_MUNICIPIO_POLO\n"
)

# Mapeamento oficial de Duração dos Cursos (em semestres) enviado pelo usuário
DURACAO_CURSOS_SEMESTRES = {
    "SBADMT": 8,  # Administração (8 semestres = 4 anos)
    "STADS": 5,   # ADS (5 semestres = 2,5 anos)
    "SBCC": 8,    # Ciências Contábeis (8 semestres = 4 anos)
    "SLPED": 8,   # Pedagogia (8 semestres = 4 anos)
    "PED": 8,     # Pedagogia (8 semestres = 4 anos)
    "STPROG": 4,  # Processos Gerenciais (4 semestres = 2 anos)
    "STGRH": 4    # Gestão de RH (4 semestres = 2 anos)
}

# Dicionário de Polos (Plano B caso o arquivo 'Relacao_de_Municipios' não esteja na pasta)
POLOS_IBGE_HARDCODED = {
    "JUIZ DE FORA": "3136702",
    "RECIFE": "2611606",
    "JUAZEIRO": "2918407",
    "TERESINA": "2211001",
    "DATAMERICA": "2211001",
    "CURITIBA": "4106902",
    "FAVI": "4106902",
    "SALVADOR": "2927408",
    "NOVA IGUACU": "3303500",
    "BRUYM VARGAS": "3303500",
    "SAO PAULO": "3550308",
    "CASA TOMBADA": "3550308",
    "SAO MIGUEL DOS CAMPOS": "2708600",
    "BELO HORIZONTE": "3106200",
    "SAVASSI": "3106200",
    "VITORIA": "3205309",
    "RIO DE JANEIRO": "3304557",
    "CONQUISTA": "3304557",
    "FEIRA DE SANTANA": "2910800",
    "GOVERNADOR VALADARES": "3127701",
    "SEDE": "3127701",
    "ILHEUS": "2913606",
    "SANTO AMARO": "2928604"
}

# ==========================================
# INTEGRAÇÃO COM API SOLIS GE (COM CACHE EM MEMÓRIA)
# ==========================================
class SolisAPI:
    """Classe para gerenciar autenticação e chamadas da API do SolisGE."""
    def __init__(self):
        self.base_url = os.getenv("SOLIS_API_URL", "https://academico.faculdadeimes.org.br").rstrip("/")
        self.user = os.getenv("SOLIS_WEB_USER", "")
        self.password = os.getenv("SOLIS_WEB_PASSWORD", "")
        self.token = os.getenv("SOLIS_JWT_TOKEN", "")
        self._cache_base_alunos = None

    def autenticar(self):
        """Realiza a autenticação dinâmica na API SolisGE para obter um JWT Token válido."""
        if not self.user or not self.password:
            print("⚠️ Usuário ou senha não encontrados no arquivo .env. Usando token salvo...")
            return bool(self.token)

        auth_url = f"{self.base_url}/api/autenticar"
        print(f"🔑 Autenticando na API SolisGE ({auth_url})...")
        try:
            resp = requests.post(
                auth_url,
                data={"user": self.user, "password": self.password},
                timeout=30
            )
            if resp.status_code == 200:
                self.token = resp.json()
                print("✅ Autenticação realizada com sucesso!")
                return True
            else:
                print(f"⚠️ Falha na autenticação HTTP {resp.status_code}: {resp.text}")
        except Exception as e:
            print(f"⚠️ Erro ao tentar autenticar no SolisGE ({e}).")
        
        return bool(self.token)

    def obter_headers(self):
        return {
            "X-Token": self.token,
            "Content-Type": "application/json"
        }

    def carregar_base_geral_alunos(self):
        """Carrega e armazena em cache a base completa de alunos ativos do SolisGE."""
        if self._cache_base_alunos is not None:
            return self._cache_base_alunos

        if not self.token:
            if not self.autenticar():
                print("❌ Erro: Não foi possível obter o Token JWT da API SolisGE.")
                return []

        url_relatorio = f"{self.base_url}/api/basico/relatorio-generico/gerar/{ID_RELATORIO_GENERICO}"
        payloads_tentativas = [{"par": {}}, {"par": {"turma": "%"}}, {"par": {"turma": None}}]

        print("📡 Solicitando dados ao SolisGE (aguarde, requisição em andamento)...")
        print("💡 Nota: A consulta ao banco do SolisGE pode levar de 1 a 3 minutos...")

        for payload in payloads_tentativas:
            try:
                resp = requests.get(
                    url_relatorio,
                    headers=self.obter_headers(),
                    json=payload,
                    timeout=HTTP_TIMEOUT_SOLIS
                )
                if resp.status_code == 200:
                    dados = resp.json()
                    res_list = []
                    if isinstance(dados, list):
                        res_list = dados
                    elif isinstance(dados, dict) and "data" in dados:
                        res_list = dados["data"]
                    
                    if res_list:
                        self._cache_base_alunos = res_list
                        print(f"✅ Conexão concluída! {len(res_list)} registros de alunos carregados da base SolisGE.")
                        return self._cache_base_alunos
                elif resp.status_code == 401:
                    print("🔄 Token expirado. Reautenticando...")
                    if self.autenticar():
                        return self.carregar_base_geral_alunos()
            except requests.exceptions.Timeout:
                print("⏳ Timeout estendido excedido no payload atual. Tentando alternativa...")
            except Exception as e:
                print(f"⚠️ Erro na conexão HTTP: {e}")

        self._cache_base_alunos = []
        return []

    def buscar_detalhes_contrato(self, person_id_ou_contrato):
        """Busca dados detalhados da pessoa/contrato via /api/academico/contrato/buscar."""
        if not person_id_ou_contrato:
            return {}

        url_contrato = f"{self.base_url}/api/academico/contrato/buscar"
        params = {"personId": str(person_id_ou_contrato)}

        try:
            resp = requests.get(url_contrato, headers=self.obter_headers(), params=params, timeout=10)
            if resp.status_code == 200 and resp.json():
                res_data = resp.json()
                if isinstance(res_data, list) and len(res_data) > 0:
                    return res_data[0]
                elif isinstance(res_data, dict):
                    return res_data
        except Exception:
            pass

        return {}

# ==========================================
# FUNÇÕES DE REGRAS E TRATAMENTO DE DADOS
# ==========================================

def remover_acentos(texto):
    """Remove acentos e deixa o texto em letras maiúsculas para facilitar cruzamentos."""
    if not texto: return ""
    texto_sem_acento = unicodedata.normalize('NFKD', str(texto)).encode('ASCII', 'ignore').decode('utf-8')
    return texto_sem_acento.upper().strip()

def limpar_cpf(cpf):
    """Remove pontos e traços do CPF e garante 11 dígitos preenchendo com zeros à esquerda."""
    cpf_numeros = re.sub(r'\D', '', str(cpf))
    return cpf_numeros.zfill(11) if cpf_numeros else ""

def validar_cpf(cpf_str):
    """Validador matemático oficial do CPF. Retorna True se for válido, False se for inválido."""
    cpf = limpar_cpf(cpf_str)
    if len(cpf) != 11 or cpf in [str(i)*11 for i in range(10)]:
        return False
    soma = sum(int(cpf[i]) * (10 - i) for i in range(9))
    digito1 = 11 - (soma % 11)
    digito1 = 0 if digito1 >= 10 else digito1
    if str(digito1) != cpf[9]: return False
    soma = sum(int(cpf[i]) * (11 - i) for i in range(10))
    digito2 = 11 - (soma % 11)
    digito2 = 0 if digito2 >= 10 else digito2
    if str(digito2) != cpf[10]: return False
    return True

def extrair_ano(texto):
    """Extrai um ano com 4 dígitos de dentro de qualquer texto."""
    match = re.search(r'\b(19\d{2}|20\d{2})\b', str(texto))
    return match.group(0) if match else ""

def encontrar_valor(linha, possiveis_nomes_coluna):
    """Busca o valor na linha do arquivo lidando com variações no nome da coluna."""
    for chave_coluna in linha.keys():
        if not chave_coluna or str(chave_coluna).strip() == "":
            continue
        chave_limpa = remover_acentos(chave_coluna)
        for nome_buscado in possiveis_nomes_coluna:
            if remover_acentos(nome_buscado) in chave_limpa:
                return str(linha[chave_coluna]).strip()
    return ""

def extrair_ano_inicio_turma(nome_turma):
    """
    REGRA 2: O ano de início se refere ao ano que iniciou a turma (ex: 202601 -> 2026, 202401 -> 2024).
    """
    match = re.search(r'\b(20\d{2})\d{2}\b', str(nome_turma))
    if match:
        return match.group(1)
    
    match_ano = re.search(r'\b(20\d{2})\b', str(nome_turma))
    if match_ano:
        return match_ano.group(1)

    return ANO_CORRENTE

def calcular_ano_fim_ensino_medio(data_nascimento_str, ano_inicio_graduacao):
    """
    REGRA 1: Data de nascimento + 17 anos (12 anos de estudos a partir dos 5/6 anos).
    Garante obrigatoriamente que ano_fim_ensino_medio < ano_inicio_graduacao (exigência INEP).
    """
    ano_nasc = extrair_ano(data_nascimento_str)
    if ano_nasc and ano_nasc.isdigit():
        ano_estimado = int(ano_nasc) + 17
    else:
        ano_estimado = int(ano_inicio_graduacao) - 8

    # Trava de segurança cronológica exigida pelo INEP (Médio < Início Graduação)
    if ano_estimado >= int(ano_inicio_graduacao):
        ano_estimado = int(ano_inicio_graduacao) - 1

    return str(ano_estimado)

def calcular_formatura(ano_inicio_str, curso_ou_turma):
    """
    REGRA 3: Duração em semestres por curso para calcular ano e semestre de formatura:
    - SBADMT: 8 semestres
    - STADS: 5 semestres
    - SBCC: 8 semestres
    - SLPED / PED: 8 semestres
    - STPROG: 4 semestres
    - STGRH: 4 semestres
    """
    ano_inicio = int(ano_inicio_str) if ano_inicio_str and ano_inicio_str.isdigit() else int(ANO_CORRENTE)
    texto_upper = remover_acentos(str(curso_ou_turma))
    
    semestres = 8  # Duração padrão
    for sigla, qtd_sem in DURACAO_CURSOS_SEMESTRES.items():
        if sigla in texto_upper:
            semestres = qtd_sem
            break

    anos_adicionais = (semestres - 1) // 2
    semestre_final = "2" if (semestres % 2 == 0) else "1"
    
    ano_formatura = ano_inicio + anos_adicionais
    return str(ano_formatura), semestre_final

def carregar_base_municipios(diretorio="."):
    """Busca um arquivo 'Relacao_de_Municipios' na pasta para mapeamento dinâmico de IBGE."""
    base_dinamica = {}
    if not diretorio: diretorio = "."

    caminho_encontrado = None
    for nome_arq in os.listdir(diretorio):
        if "relacao_de_municipios" in remover_acentos(nome_arq).lower() and nome_arq.endswith(('.xlsx', '.xls', '.csv')):
            caminho_encontrado = os.path.join(diretorio, nome_arq)
            break

    if not caminho_encontrado:
        return base_dinamica

    print(f"📖 Base oficial do IBGE encontrada: Carregando municípios de '{os.path.basename(caminho_encontrado)}'...")
    try:
        if caminho_encontrado.endswith(('.xlsx', '.xls')):
            import pandas as pd
            df = pd.read_excel(caminho_encontrado, dtype=str).fillna("")
            for _, linha in df.iterrows():
                linha_dict = {str(k): str(v) for k, v in linha.items()}
                nome_mun = encontrar_valor(linha_dict, ['nome do municipio', 'municipio'])
                cod_mun = encontrar_valor(linha_dict, ['co_municipio', 'codigo', 'ibge'])
                if nome_mun and cod_mun:
                    base_dinamica[remover_acentos(nome_mun)] = re.sub(r'\D', '', cod_mun)
        else:
            with open(caminho_encontrado, 'r', encoding='utf-8', errors='ignore') as f:
                leitor = csv.DictReader(f, delimiter=',')
                for linha in leitor:
                    nome_mun = encontrar_valor(linha, ['nome do municipio', 'municipio'])
                    cod_mun = encontrar_valor(linha, ['co_municipio', 'codigo', 'ibge'])
                    if nome_mun and cod_mun:
                        base_dinamica[remover_acentos(nome_mun)] = re.sub(r'\D', '', cod_mun)
    except Exception as e:
        print(f"⚠️ Erro ao ler base de municípios ({e}). O sistema usará o Dicionário Padrão.")

    return base_dinamica

def buscar_ibge(nome_polo, base_municipios_dinamica):
    """Busca o código do IBGE baseado no nome do polo, cruzando com a base oficial ou hardcoded."""
    if not nome_polo: return ""

    match_parenteses = re.search(r'\((.*?)\)', str(nome_polo))
    if match_parenteses:
        cidade_uf = match_parenteses.group(1)
        cidade = cidade_uf.split('-')[0].strip()
        cidade_limpa = remover_acentos(cidade)
        if cidade_limpa in base_municipios_dinamica:
            return base_municipios_dinamica[cidade_limpa]

    polo_upper = remover_acentos(str(nome_polo))
    for chave, codigo in POLOS_IBGE_HARDCODED.items():
        if chave in polo_upper:
            return codigo

    return ""

def obter_proximo_sequencial(diretorio, prefixo_base, co_curso):
    """Verifica os arquivos já existentes na pasta e retorna o próximo sequencial disponível."""
    if not diretorio:
        diretorio = "."

    padrao_busca = f"{prefixo_base}_{co_curso}_E"
    maior_sequencial = 0

    for nome_arquivo in os.listdir(diretorio):
        if nome_arquivo.startswith(padrao_busca) and (nome_arquivo.endswith(".txt") or nome_arquivo.endswith(".zip")):
            parte_final = nome_arquivo.replace(padrao_busca, "").replace(".txt", "").replace(".zip", "")
            if parte_final.isdigit():
                numero = int(parte_final)
                if numero > maior_sequencial:
                    maior_sequencial = numero

    proximo_numero = maior_sequencial + 1
    return f"E{proximo_numero:03d}"

def identificar_codigo_curso(nome_curso_ou_arquivo):
    """Identifica o código do curso e-MEC com base no nome do curso ou arquivo."""
    nome_upper = remover_acentos(str(nome_curso_ou_arquivo))
    if "ADS" in nome_upper or "ANALISE E DESENVOLVIMENTO" in nome_upper:
        return "1587876", "ADS (Análise e Desenvolvimento de Sistemas)"
    elif "PED" in nome_upper or "PEDAGOGIA" in nome_upper:
        return "1386384", "PED (Pedagogia)"
    return "0000000", "DESCONHECIDO"

def sanitizar_nome_arquivo(nome):
    """Remove caracteres inválidos para nomes de arquivos."""
    return re.sub(r'[\\/*?:"<>|]', '_', str(nome)).strip()

# ==========================================
# PROCESSAMENTO DOS ALUNOS DA TURMA SELECIONADA
# ==========================================

def processar_alunos_turma(nome_turma_oficial, alunos_lista, api_obj):
    """
    Recebe os alunos de uma turma específica, enriquece via API contrato,
    aplica as 3 REGRAS DINÂMICAS (Ano Médio, Ano Início, Previsão de Formatura),
    extrai o Polo do contrato, gera a PLANILHA 1 (Entrada) e a PLANILHA 2 (Modelo ENADE + ZIP).
    """
    if not alunos_lista:
        print(f"⚠️ Nenhum aluno encontrado para a turma '{nome_turma_oficial}'.")
        return

    print(f"\n🔄 Enriquecendo cadastro dos {len(alunos_lista)} alunos da turma '{nome_turma_oficial}' via API SolisGE...")
    alunos_enriquecidos = []
    
    # REGRA 2: Ano de Início extraído da Turma (ex: 202601 -> 2026)
    ano_inicio_turma = extrair_ano_inicio_turma(nome_turma_oficial)

    for item in alunos_lista:
        matricula = item.get("Matrícula") or item.get("codigo_aluno") or item.get("personId") or ""
        contrato = item.get("Contrato") or item.get("codigo_contrato") or ""
        nome = item.get("Aluno") or item.get("nome_aluno") or item.get("personName") or ""
        cpf = item.get("CPF") or item.get("cpf") or ""
        turma = item.get("Turma") or str(nome_turma_oficial)
        curso = item.get("Curso") or ""
        turno = item.get("Turno") or ""
        situacao = item.get("Situação") or ""
        if "VESTIBULANDO" in remover_acentos(situacao):
            continue

        email = item.get("Email") or ""
        telefone = item.get("Telefone celular") or item.get("Telefone residencial") or ""
        data_nascimento = item.get("Data de Nascimento") or item.get("dateBirth") or ""

        polo_relatorio = item.get("Polo") or item.get("polo") or item.get("Unidade") or item.get("unidade") or ""

        # Consulta detalhes do contrato na API do SolisGE caso faltem dados
        detalhes = api_obj.buscar_detalhes_contrato(matricula if matricula else contrato)
        polo_contrato = ""

        if detalhes:
            polo_contrato = (
                detalhes.get("unitDescription") or 
                detalhes.get("centerDescription") or 
                detalhes.get("polo") or 
                detalhes.get("poloName") or ""
            )

            person_data = detalhes.get("personData", {})
            if person_data:
                if not cpf: cpf = person_data.get("cpf", "")
                if not email: email = person_data.get("email", "")
                if not telefone: telefone = person_data.get("cellPhone", "")
                if not data_nascimento: data_nascimento = person_data.get("dateBirth", "")

            if not curso and detalhes.get("courseName"):
                curso = detalhes.get("courseName")

        # Polo prioritário
        polo_final = polo_relatorio or polo_contrato or turma

        # ---------------------------------------------------------
        # APLICAÇÃO DAS 3 REGRAS SOLICITADAS PELO USUÁRIO:
        # ---------------------------------------------------------
        # REGRA 1: Conclusão do Ensino Médio = Data Nascimento + 17 anos (com trava Ensino Médio < Início)
        ano_conclusao_medio = calcular_ano_fim_ensino_medio(data_nascimento, ano_inicio_turma)

        # REGRA 2: Ano de Início Graduação = Ano da Turma (ex: 2026)
        ano_inicio_graduacao = ano_inicio_turma

        # REGRA 3: Previsão de Formatura baseada na Duração do Curso em Semestres
        ano_formatura_est, semestre_formatura_est = calcular_formatura(ano_inicio_graduacao, curso or turma)

        alunos_enriquecidos.append({
            "Turma": turma,
            "Contrato": contrato,
            "Matrícula": matricula,
            "Aluno": nome,
            "CPF": cpf,
            "Data de Nascimento": data_nascimento,
            "Email": email,
            "Telefone": telefone,
            "Curso": curso,
            "Turno": turno,
            "Situação": situacao,
            "Ano de Inicio": ano_inicio_graduacao,
            "Conclusao Ensino Medio": ano_conclusao_medio,
            "Previsão de Conclusao": ano_formatura_est,
            "Semestre": semestre_formatura_est,
            "Polo": polo_final
        })

    sufixo_arq = sanitizar_nome_arquivo(nome_turma_oficial)

    # 1. GERAR PLANILHA 1 (Primeira Base - Entrada Cadastral)
    nome_p1 = f"Planilha1_Entrada_Turma_{sufixo_arq}.xlsx"
    try:
        import pandas as pd
        df_p1 = pd.DataFrame(alunos_enriquecidos)
        df_p1.to_excel(nome_p1, index=False)
        print(f"\n📊 PLANILHA 1 (Primeira Base - Entrada Cadastral) gerada: '{nome_p1}'")
    except Exception as e:
        print(f"⚠️ Erro ao salvar Excel para Planilha 1: {e}. Salvando em CSV...")
        nome_p1 = f"Planilha1_Entrada_Turma_{sufixo_arq}.csv"
        keys = alunos_enriquecidos[0].keys()
        with open(nome_p1, 'w', newline='', encoding='utf-8') as f:
            dict_writer = csv.DictWriter(f, fieldnames=keys)
            dict_writer.writeheader()
            dict_writer.writerows(alunos_enriquecidos)
        print(f"📊 PLANILHA 1 (Primeira Base - Entrada Cadastral) gerada em CSV: '{nome_p1}'")

    # 2. GERAR PLANILHA 2 (Segunda Base Convertida ENADE + ZIP INEP)
    print(f"⚡ Convertendo primeira base para a PLANILHA 2 (Modelo ENADE)...")
    processar_arquivo(nome_p1, codigo_turma_override=sufixo_arq)

def processar_arquivo(caminho_entrada, codigo_turma_override=None):
    """Processa o arquivo de entrada e gera a Planilha 2 convertida e o arquivo ZIP no padrão INEP."""

    caminho_entrada = caminho_entrada.strip()
    if caminho_entrada.startswith("& "): caminho_entrada = caminho_entrada[2:].strip()
    caminho_entrada = caminho_entrada.strip("'").strip('"')

    if not os.path.exists(caminho_entrada):
        print(f"\n❌ Erro: O arquivo não foi encontrado:\n{caminho_entrada}")
        return

    diretorio_atual = os.path.dirname(caminho_entrada)
    nome_base, extensao = os.path.splitext(os.path.basename(caminho_entrada))

    co_curso_atual, nome_curso_identificado = identificar_codigo_curso(codigo_turma_override or nome_base)

    diretorio_saida = diretorio_atual if diretorio_atual else "."
    sufixo_sequencial = obter_proximo_sequencial(diretorio_saida, PREFIXO_NOME_ARQUIVO, co_curso_atual)

    nome_arquivo_saida = f"{PREFIXO_NOME_ARQUIVO}_{co_curso_atual}_{sufixo_sequencial}.txt"
    caminho_saida_txt = os.path.join(diretorio_saida, nome_arquivo_saida)

    nome_arquivo_zip = f"{PREFIXO_NOME_ARQUIVO}_{co_curso_atual}_{sufixo_sequencial}.zip"
    caminho_saida_zip = os.path.join(diretorio_saida, nome_arquivo_zip)

    linhas_para_processar = []
    base_municipios = carregar_base_municipios(diretorio_atual)

    try:
        if extensao.lower() in ['.xlsx', '.xls']:
            try:
                import pandas as pd
                print(f"⏳ Lendo Planilha 1 ({caminho_entrada})...")
                df = pd.read_excel(caminho_entrada)
                df = df.fillna("")
                linhas_para_processar = df.to_dict('records')
            except ImportError:
                print("\n📦 Instale o pandas para ler Excel nativo: pip install pandas openpyxl xlrd")
                return
        else:
            print(f"⏳ Lendo arquivo de texto ({caminho_entrada})...")
            delimitador = ','
            with open(caminho_entrada, 'r', encoding='utf-8') as f:
                primeira_linha = f.readline()
                if '\t' in primeira_linha: delimitador = '\t'
                elif ';' in primeira_linha: delimitador = ';'

            with open(caminho_entrada, mode='r', encoding='utf-8', errors='ignore') as arquivo_in:
                leitor = csv.DictReader(arquivo_in, delimiter=delimitador)
                linhas_para_processar = list(leitor)

        linhas_processadas = 0
        correcoes_aplicadas = {
            "cpf": 0,
            "ano_medio": 0,
            "ano_inicio": 0,
            "polo_ibge": 0,
            "semestre_corrente": 0,
            "cronologia_ajustada": 0
        }
        alunos_cpf_invalido = []
        linhas_convertidas_p2 = []

        with open(caminho_saida_txt, mode='w', encoding='utf-8') as arquivo_out:
            arquivo_out.write(CABECHALHO_INEP)

            for linha in linhas_para_processar:
                linha_str = {str(k): str(v) for k, v in linha.items()}

                nome_bruto = encontrar_valor(linha_str, ['nome', 'aluno'])
                if not nome_bruto: nome_bruto = "Aluno Desconhecido"

                cpf_bruto = encontrar_valor(linha_str, ['cpf'])
                data_nasc_bruto = encontrar_valor(linha_str, ['data de nascimento', 'nascimento'])
                turma_bruta = encontrar_valor(linha_str, ['turma'])
                curso_bruto = encontrar_valor(linha_str, ['curso'])
                polo_bruto = encontrar_valor(linha_str, ['polo', 'local'])

                ano_medio_bruto = encontrar_valor(linha_str, ['conclusao ensino', 'ensino medio'])
                ano_inicio_bruto = encontrar_valor(linha_str, ['inicio graduacao', 'ano de inicio', 'ingresso', 'inicio'])
                ano_formatura_bruto = encontrar_valor(linha_str, ['previsao de conclusao', 'previsao'])
                semestre_bruto = encontrar_valor(linha_str, ['semestre'])

                if co_curso_atual == "0000000" and curso_bruto:
                    co_curso_atual, nome_curso_identificado = identificar_codigo_curso(curso_bruto)

                if not cpf_bruto or str(cpf_bruto).strip() == "":
                    continue

                # 1. CPF (Extração e Validação Matemática)
                cpf_limpo = limpar_cpf(cpf_bruto)
                if not cpf_limpo: continue

                if not validar_cpf(cpf_limpo):
                    alunos_cpf_invalido.append(f"{nome_bruto} (CPF: {cpf_bruto})")

                if len(limpar_cpf(cpf_bruto)) < 11:
                    correcoes_aplicadas["cpf"] += 1

                # REGRA 2: Ano de Início
                ano_inicio = extrair_ano(ano_inicio_bruto) or extrair_ano_inicio_turma(turma_bruta)

                # REGRA 1: Ano Fim Ensino Médio
                ano_medio = extrair_ano(ano_medio_bruto) or calcular_ano_fim_ensino_medio(data_nasc_bruto, ano_inicio)

                # Validação Cronológica Obrigatória
                if int(ano_medio) >= int(ano_inicio):
                    ano_medio = str(int(ano_inicio) - 1)
                    correcoes_aplicadas["cronologia_ajustada"] += 1

                # REGRA 3: Formatura (Ano e Semestre)
                if ano_formatura_bruto and extrair_ano(ano_formatura_bruto):
                    ano_formatura = extrair_ano(ano_formatura_bruto)
                    semestre_formatura = semestre_bruto if semestre_bruto in ['1', '2'] else "2"
                else:
                    ano_formatura, semestre_formatura = calcular_formatura(ano_inicio, curso_bruto or turma_bruta)

                # 7. Código IBGE do Polo
                codigo_ibge = buscar_ibge(polo_bruto, base_municipios)
                if not codigo_ibge:
                    codigo_ibge = IBGE_SEDE
                    correcoes_aplicadas["polo_ibge"] += 1

                # Linha formatada exigida pelo INEP
                linha_formatada = (
                    f"{CO_PROJETO};{TP_ORIGEM};{CO_IES};{co_curso_atual};{cpf_limpo};"
                    f"{ano_medio};{CO_TURNO_GRADUACAO};{NU_PERCENTUAL_INTEGRALIZACAO};"
                    f"{ano_formatura};{semestre_formatura};{ano_inicio};"
                    f"{IN_MUNICIPIO_POLO_EXTERIOR};{codigo_ibge}\n"
                )

                arquivo_out.write(linha_formatada)
                linhas_processadas += 1

                # Guarda registro formatado para a Planilha 2
                linhas_convertidas_p2.append({
                    "CO_PROJETO": CO_PROJETO,
                    "TP_ORIGEM": TP_ORIGEM,
                    "CO_IES": CO_IES,
                    "CO_CURSO": co_curso_atual,
                    "NU_CPF": cpf_limpo,
                    "NU_ANO_FIM_ENSINO_MEDIO": ano_medio,
                    "CO_TURNO_GRADUACAO": CO_TURNO_GRADUACAO,
                    "NU_PERCENTUAL_INTEGRALIZACAO": NU_PERCENTUAL_INTEGRALIZACAO,
                    "NU_ANO_FORMATURA": ano_formatura,
                    "NU_SEMESTRE_FORMATURA": semestre_formatura,
                    "NU_ANO_INICIO_GRADUACAO": ano_inicio,
                    "IN_MUNICIPIO_POLO_EXTERIOR": IN_MUNICIPIO_POLO_EXTERIOR,
                    "CO_MUNICIPIO_POLO": codigo_ibge
                })

        # Compacta no arquivo ZIP oficial do INEP
        with zipfile.ZipFile(caminho_saida_zip, 'w', zipfile.ZIP_DEFLATED) as zipf:
            zipf.write(caminho_saida_txt, arcname=nome_arquivo_saida)

        os.remove(caminho_saida_txt)

        # SALVAR PLANILHA 2 (CONVERTIDA MODELO ENADE)
        sufixo_p2 = f"Turma_{codigo_turma_override}" if codigo_turma_override else nome_base
        nome_p2_excel = f"Planilha2_Convertida_ENADE_{sufixo_p2}.xlsx"
        try:
            import pandas as pd
            df_p2 = pd.DataFrame(linhas_convertidas_p2)
            df_p2.to_excel(nome_p2_excel, index=False)
            print(f"📊 PLANILHA 2 (Segunda Base - Modelo Convertido ENADE) gerada: '{nome_p2_excel}'")
        except Exception as e:
            print(f"⚠️ Erro ao salvar Excel para Planilha 2: {e}")

        print("\n" + "=" * 50)
        print(f"✅ ARQUIVO ZIP GERADO E PRONTO PARA UPLOAD NO INEP!")
        print(f"📄 Arquivo ZIP: {nome_arquivo_zip}")
        print(f"🎓 Curso Identificado: {nome_curso_identificado} (Código: {co_curso_atual})")
        print(f"📊 Alunos processados: {linhas_processadas}")
        print("-" * 50)

        if sum(correcoes_aplicadas.values()) > 0:
            print("🛡️  SISTEMA SALVA-VIDAS ATUOU NAS SEGUINTES FALHAS DA PLANILHA:")
            if correcoes_aplicadas["cpf"] > 0:
                print(f"   👉 {correcoes_aplicadas['cpf']} CPF(s) sem zeros à esquerda foram corrigidos.")
            if correcoes_aplicadas["cronologia_ajustada"] > 0:
                print(f"   👉 {correcoes_aplicadas['cronologia_ajustada']} aluno(s) tinham Ano do Médio >= Ano do Início. Ajustado automaticamente!")
            if correcoes_aplicadas["polo_ibge"] > 0:
                print(f"   👉 {correcoes_aplicadas['polo_ibge']} aluno(s) com Polo desconhecido. Direcionado para IBGE da Sede ({IBGE_SEDE}).")

        if alunos_cpf_invalido:
            print("\n❌ ALERTA CRÍTICO: OS SEGUINTES ALUNOS POSSUEM CPF INVÁLIDO NA RECEITA FEDERAL!")
            print("O Inep irá rejeitar essas linhas se enviadas:")
            for aluno_invalido in alunos_cpf_invalido:
                print(f"   ⚠️ {aluno_invalido}")

        print("=" * 50)

    except Exception as e:
        print(f"\n❌ Ocorreu um erro durante o processamento: {e}")

# ==========================================
# INTERFACE PRINCIPAL INTERATIVA (2 ETAPAS POR CÓDIGO)
# ==========================================

def executar_interface_principal():
    """
    1. Solicita o código/termo de busca da turma (ex: 202401, 202601, STADS).
    2. Conecta ao SolisGE (carregando a base com cache).
    3. Identifica e exibe todas as turmas que possuem o código informado.
    4. Permite a escolha da turma para buscar os alunos vinculados e gerar a Planilha 1 e Planilha 2.
    """
    print("\n" + "=" * 65)
    print("🤖 SISTEMA INTEGRADO SOLIS-GE -> ENADE 2026 (IMES) 🤖")
    print("=" * 65)

    api = SolisAPI()

    while True:
        try:
            print("-" * 65)
            termo_busca = input(
                "📁 Digite o CÓDIGO/NÚMERO DA TURMA que deseja pesquisar\n"
                "   (ex: 202401, 202601, 202602 ou STADS)\n"
                "   OU arraste uma planilha local (Excel/CSV)\n"
                "   (Digite 'sair' para encerrar): \n> "
            ).strip()

            if termo_busca.lower() in ['sair', 'exit', 'cancelar', 'quit']:
                print("\n🛑 Programa encerrado. Até a próxima!")
                break

            if not termo_busca:
                continue

            # 1. Verifica se o usuário arrastou um arquivo local
            caminho_limpo = termo_busca.strip("'").strip('"')
            if caminho_limpo.startswith("& "): caminho_limpo = caminho_limpo[2:].strip()

            if os.path.exists(caminho_limpo) and os.path.isfile(caminho_limpo):
                print(f"\n📂 Arquivo local detectado: {caminho_limpo}")
                processar_arquivo(caminho_limpo)
                continue

            # 2. Carrega base de dados do SolisGE (se ainda não carregada)
            alunos_todos = api.carregar_base_geral_alunos()

            if not alunos_todos:
                print("⚠️ Não foi possível obter os dados do SolisGE via API.")
                continue

            # 3. Filtra as turmas existentes que contêm o código informado
            termo_upper = remover_acentos(termo_busca)
            turmas_encontradas = {}

            for aluno in alunos_todos:
                nome_turma = aluno.get("Turma") or aluno.get("codigo_turma") or ""
                situacao = remover_acentos(aluno.get("Situação") or aluno.get("situacao") or "")

                # REGRA DE NEGÓCIO: Desconsidera alunos que possuem status VESTIBULANDO
                if "VESTIBULANDO" in situacao:
                    continue

                if nome_turma and termo_upper in remover_acentos(nome_turma):
                    if nome_turma not in turmas_encontradas:
                        turmas_encontradas[nome_turma] = []
                    turmas_encontradas[nome_turma].append(aluno)

            lista_turmas = sorted(turmas_encontradas.keys())

            if not lista_turmas:
                print(f"\n❌ Nenhum registro de turma encontrado contendo o código '{termo_busca}'.")
                print("💡 Tente digitar um código diferente (ex: 202401, 202601, 202602).")
                continue

            # 4. Exibe as turmas encontradas para o código digitado
            print(f"\n📋 TURMAS ENCONTRADAS CONTENDO '{termo_busca}':")
            print("-" * 65)
            for idx, t_nome in enumerate(lista_turmas, start=1):
                qtd = len(turmas_encontradas[t_nome])
                print(f"   [{idx}] {t_nome} ({qtd} alunos)")
            print(f"   [T] Processar TODAS as {len(lista_turmas)} turmas listadas acima")
            print("-" * 65)

            # 5. Seleção da turma desejada
            opcao_sel = input(
                "👉 Digite o NÚMERO da turma que deseja filtrar os alunos (ex: 1 ou 1, 2 ou T para todas): \n> "
            ).strip()

            if opcao_sel.lower() in ['sair', 'exit', 'cancelar', 'quit']:
                print("\n🛑 Operação cancelada.")
                continue

            turmas_selecionadas = []
            if opcao_sel.upper() in ['T', 'TODAS', 'ALL', '%']:
                turmas_selecionadas = lista_turmas
            else:
                partes = [p.strip() for p in opcao_sel.split(',') if p.strip()]
                for p in partes:
                    if p.isdigit():
                        idx_num = int(p) - 1
                        if 0 <= idx_num < len(lista_turmas):
                            turmas_selecionadas.append(lista_turmas[idx_num])

            if not turmas_selecionadas:
                print("⚠️ Nenhuma opção válida foi selecionada.")
                continue

            # 6. Processa cada turma selecionada (Gera Planilha 1 + Planilha 2 + ZIP)
            print(f"\n🚀 Processando {len(turmas_selecionadas)} turma(s) selecionada(s)...")
            for t_nome in turmas_selecionadas:
                alunos_turma = turmas_encontradas[t_nome]
                print(f"\n------------------------------------------------------------")
                print(f"🎯 Turma Selecionada: '{t_nome}'")
                print(f"------------------------------------------------------------")
                processar_alunos_turma(t_nome, alunos_turma, api)

        except KeyboardInterrupt:
            print("\n\n🛑 Programa interrompido pelo usuário. Até logo!")
            break

if __name__ == "__main__":
    executar_interface_principal()