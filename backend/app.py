from flask import Flask, request, jsonify, render_template, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, login_user, logout_user, login_required, current_user, UserMixin
from sqlalchemy import inspect, text
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, date
from urllib.parse import urlparse
import os

def build_database_uri():
    database_url = os.getenv('DATABASE_URL')
    if database_url:
        if database_url.startswith('postgres://'):
            database_url = database_url.replace('postgres://', 'postgresql://', 1)
        return database_url
    return 'sqlite:///precificacao.db'


app = Flask(__name__, template_folder='../frontend/templates', static_folder='../frontend/static')
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'custo-chef-dev-secret')
app.config['SQLALCHEMY_DATABASE_URI'] = build_database_uri()
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {'pool_pre_ping': True}

db = SQLAlchemy(app)
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'


def to_float(value, default=0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


UNIT_FACTORS = {
    'kg': 1000, 'g': 1,
    'l': 1000, 'L': 1000, 'ml': 1,
    'unidade': 1, 'un': 1,
    'colher_sopa': 15, 'colher_cha': 5, 'xicara': 240
}


def get_unit_factor(unit):
    if not unit:
        return 1
    return UNIT_FACTORS.get(unit.lower(), 1)


def convert_quantity(quantity, from_unit, to_unit):
    from_factor = get_unit_factor(from_unit)
    to_factor = get_unit_factor(to_unit)
    quantidade_base = to_float(quantity) * from_factor
    return quantidade_base / to_factor if to_factor else 0


def unit_cost_from_purchase(preco_compra, quantidade_compra, unidade_compra):
    quantidade_base = convert_quantity(quantidade_compra, unidade_compra, 'g' if str(unidade_compra).lower() in ['kg', 'g'] else ('ml' if str(unidade_compra).lower() in ['l', 'ml'] else 'unidade'))
    return to_float(preco_compra) / quantidade_base if quantidade_base > 0 else 0


def calcular_baixa_estoque(produto, quantidade):
    movimentos = []
    for insumo in produto.receita.insumos:
        assoc = db.session.execute(
            receita_insumos.select().where(
                receita_insumos.c.receita_id == produto.receita.id,
                receita_insumos.c.insumo_id == insumo.id
            )
        ).fetchone()
        if assoc:
            quantidade_usada = convert_quantity(
                assoc.quantidade * quantidade,
                assoc.unidade,
                insumo.unidade_compra
            )
            movimentos.append((insumo, quantidade_usada))
    return movimentos


def aplicar_movimento_estoque(movimentos, restaurar=False):
    for insumo, quantidade in movimentos:
        if restaurar:
            insumo.estoque_atual = (insumo.estoque_atual or 0) + quantidade
        else:
            insumo.estoque_atual = max(0, (insumo.estoque_atual or 0) - quantidade)


def aplicar_compra_no_estoque(insumo, quantidade_compra, unidade_compra, remover=False):
    quantidade_convertida = convert_quantity(quantidade_compra, unidade_compra, insumo.unidade_compra)
    if remover:
        insumo.estoque_atual = max(0, (insumo.estoque_atual or 0) - quantidade_convertida)
    else:
        insumo.estoque_atual = (insumo.estoque_atual or 0) + quantidade_convertida


def sincronizar_insumo_com_ultima_compra(insumo):
    ultima = insumo.ultima_compra
    if not ultima:
        return
    insumo.fornecedor_id = ultima.fornecedor_id
    insumo.fornecedor_nome_manual = ultima.fornecedor_nome

# ============== MODELOS ==============

class User(UserMixin, db.Model):
    __tablename__ = 'usuarios'
    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class Fornecedor(db.Model):
    __tablename__ = 'fornecedores'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=False)
    nome = db.Column(db.String(100))
    cnpj = db.Column(db.String(20))
    telefone = db.Column(db.String(20))
    email = db.Column(db.String(120))
    endereco = db.Column(db.String(200))
    cidade = db.Column(db.String(50))
    estado = db.Column(db.String(2))
    cep = db.Column(db.String(10))
    contato = db.Column(db.String(100))
    observacoes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class Insumo(db.Model):
    __tablename__ = 'insumos'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=False)
    fornecedor_id = db.Column(db.Integer, db.ForeignKey('fornecedores.id'))
    fornecedor_nome_manual = db.Column('fornecedor', db.String(100))
    nome = db.Column(db.String(100), nullable=False)
    categoria = db.Column(db.String(50), nullable=False)
    unidade_compra = db.Column(db.String(20), nullable=False)
    quantidade_compra = db.Column(db.Float, nullable=False)
    preco_compra = db.Column(db.Float, nullable=False)
    estoque_atual = db.Column(db.Float, default=0)
    estoque_minimo = db.Column(db.Float, default=0)
    
    fornecedor = db.relationship('Fornecedor')
    compras = db.relationship('CompraInsumo', backref='insumo', cascade='all, delete-orphan', lazy=True)

    @property
    def fornecedor_nome_exibicao(self):
        if self.fornecedor:
            return self.fornecedor.nome
        return self.fornecedor_nome_manual or 'Sem fornecedor'
    
    @property
    def ultima_compra(self):
        if not self.compras:
            return None
        return sorted(
            self.compras,
            key=lambda compra: (compra.data_compra or date.min, compra.id or 0),
            reverse=True
        )[0]

    @property
    def custo_unitario_base_legacy(self):
        fator = get_unit_factor(self.unidade_compra)
        quantidade_base = self.quantidade_compra * fator
        return self.preco_compra / quantidade_base if quantidade_base > 0 else 0

    @property
    def custo_unitario_ultimo(self):
        ultima = self.ultima_compra
        return ultima.custo_unitario_base if ultima else self.custo_unitario_base_legacy

    @property
    def custo_unitario_medio(self):
        if not self.compras:
            return self.custo_unitario_base_legacy

        total_pago = sum(compra.preco_compra or 0 for compra in self.compras)
        total_quantidade_base = sum(compra.quantidade_base or 0 for compra in self.compras)
        return total_pago / total_quantidade_base if total_quantidade_base > 0 else self.custo_unitario_base_legacy

    @property
    def melhor_preco_unitario(self):
        if not self.compras:
            return self.custo_unitario_base_legacy
        return min(compra.custo_unitario_base for compra in self.compras if compra.custo_unitario_base is not None)

    @property
    def custo_unitario_base(self):
        return self.custo_unitario_medio
    
    def get_custo_por_unidade(self, unidade_destino):
        return self.custo_unitario_base * get_unit_factor(unidade_destino)


class CompraInsumo(db.Model):
    __tablename__ = 'compras_insumos'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=False)
    insumo_id = db.Column(db.Integer, db.ForeignKey('insumos.id'), nullable=False)
    fornecedor_id = db.Column(db.Integer, db.ForeignKey('fornecedores.id'))
    fornecedor_nome = db.Column(db.String(100))
    quantidade_compra = db.Column(db.Float, nullable=False)
    unidade_compra = db.Column(db.String(20), nullable=False)
    preco_compra = db.Column(db.Float, nullable=False)
    impacta_estoque = db.Column(db.Boolean, default=True)
    data_compra = db.Column(db.Date, default=date.today)
    observacoes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    fornecedor = db.relationship('Fornecedor')

    @property
    def fornecedor_nome_exibicao(self):
        if self.fornecedor:
            return self.fornecedor.nome
        return self.fornecedor_nome or 'Sem fornecedor'

    @property
    def quantidade_base(self):
        return to_float(self.quantidade_compra) * get_unit_factor(self.unidade_compra)

    @property
    def custo_unitario_base(self):
        return self.preco_compra / self.quantidade_base if self.quantidade_base > 0 else 0

receita_insumos = db.Table('receita_insumos',
    db.Column('receita_id', db.Integer, db.ForeignKey('receitas.id'), primary_key=True),
    db.Column('insumo_id', db.Integer, db.ForeignKey('insumos.id'), primary_key=True),
    db.Column('quantidade', db.Float, nullable=False),
    db.Column('unidade', db.String(20), nullable=False)
)

class Receita(db.Model):
    __tablename__ = 'receitas'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=False)
    nome = db.Column(db.String(100), nullable=False)
    categoria = db.Column(db.String(50))
    rendimento_teorico = db.Column(db.Integer, default=1)
    perda_percentual = db.Column(db.Float, default=0)
    tempo_preparo = db.Column(db.Integer)
    insumos = db.relationship('Insumo', secondary=receita_insumos, backref='receitas')
    
    @property
    def rendimento_real(self):
        return self.rendimento_teorico * (1 - self.perda_percentual / 100)
    
    @property
    def custo_total(self):
        total = 0
        for insumo in self.insumos:
            assoc = db.session.execute(
                receita_insumos.select().where(
                    receita_insumos.c.receita_id == self.id,
                    receita_insumos.c.insumo_id == insumo.id
                )
            ).fetchone()
            if assoc:
                custo_item = insumo.get_custo_por_unidade(assoc.unidade) * assoc.quantidade
                total += custo_item
        return total
    
    @property
    def custo_unitario(self):
        if self.rendimento_real > 0:
            return self.custo_total / self.rendimento_real
        return self.custo_total

class Produto(db.Model):
    __tablename__ = 'produtos'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=False)
    receita_id = db.Column(db.Integer, db.ForeignKey('receitas.id'), nullable=False)
    nome = db.Column(db.String(100), nullable=False)
    custo_real = db.Column(db.Float, nullable=False)
    margem_lucro = db.Column(db.Float, default=30)
    markup = db.Column(db.Float)
    taxa_venda = db.Column(db.Float, default=0)
    preco_venda = db.Column(db.Float)
    ativo = db.Column(db.Boolean, default=True)
    
    receita = db.relationship('Receita')

    @property
    def custo_real_atual(self):
        return self.receita.custo_unitario if self.receita else self.custo_real

    def calcular_preco_com_custo(self, custo_base):
        if self.markup:
            preco = custo_base * self.markup
        else:
            margem_decimal = self.margem_lucro / 100
            if margem_decimal >= 1:
                margem_decimal = 0.99
            preco = custo_base / (1 - margem_decimal)

        if self.taxa_venda > 0:
            preco = preco / (1 - self.taxa_venda / 100)

        return round(preco, 2)
    
    def calcular_preco(self):
        return self.calcular_preco_com_custo(self.custo_real)
    
    @property
    def lucro_unitario(self):
        return (self.preco_venda or self.calcular_preco()) - self.custo_real
    
    @property
    def margem_real(self):
        preco = self.preco_venda or self.calcular_preco()
        if preco > 0:
            return (self.lucro_unitario / preco) * 100
        return 0

    @property
    def lucro_unitario_atual(self):
        preco = self.preco_venda or self.calcular_preco_com_custo(self.custo_real_atual)
        return preco - self.custo_real_atual

    @property
    def margem_real_atual(self):
        preco = self.preco_venda or self.calcular_preco_com_custo(self.custo_real_atual)
        if preco > 0:
            return (self.lucro_unitario_atual / preco) * 100
        return 0

    @property
    def margem_caiu(self):
        return self.margem_real_atual + 0.01 < self.margem_lucro

class Venda(db.Model):
    __tablename__ = 'vendas'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=False)
    produto_id = db.Column(db.Integer, db.ForeignKey('produtos.id'), nullable=False)
    quantidade = db.Column(db.Integer, default=1)
    preco_unitario = db.Column(db.Float, nullable=False)
    valor_total = db.Column(db.Float, nullable=False)
    forma_pagamento = db.Column(db.String(50))
    tem_entrega = db.Column(db.Boolean, default=False)
    valor_entrega = db.Column(db.Float, default=0)
    custo_total = db.Column(db.Float)
    lucro_total = db.Column(db.Float)
    data_venda = db.Column(db.Date, default=date.today)
    observacoes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    produto = db.relationship('Produto')

class CustoFixo(db.Model):
    __tablename__ = 'custos_fixos'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=False)
    descricao = db.Column(db.String(100), nullable=False)
    valor_mensal = db.Column(db.Float, nullable=False)
    categoria = db.Column(db.String(50))
    dia_vencimento = db.Column(db.Integer)
    ativo = db.Column(db.Boolean, default=True)


def ensure_sqlite_schema_compatibility():
    if not app.config['SQLALCHEMY_DATABASE_URI'].startswith('sqlite'):
        return

    inspector = inspect(db.engine)

    if 'insumos' in inspector.get_table_names():
        insumo_columns = {column['name'] for column in inspector.get_columns('insumos')}
        if 'fornecedor_id' not in insumo_columns:
            db.session.execute(text('ALTER TABLE insumos ADD COLUMN fornecedor_id INTEGER'))

    if 'produtos' in inspector.get_table_names():
        produto_columns = {column['name'] for column in inspector.get_columns('produtos')}
        if 'ativo' not in produto_columns:
            db.session.execute(text('ALTER TABLE produtos ADD COLUMN ativo BOOLEAN DEFAULT 1'))

    if 'custos_fixos' in inspector.get_table_names():
        custos_columns = {column['name'] for column in inspector.get_columns('custos_fixos')}
        if 'categoria' not in custos_columns:
            db.session.execute(text('ALTER TABLE custos_fixos ADD COLUMN categoria VARCHAR(50)'))
        if 'dia_vencimento' not in custos_columns:
            db.session.execute(text('ALTER TABLE custos_fixos ADD COLUMN dia_vencimento INTEGER'))
        if 'ativo' not in custos_columns:
            db.session.execute(text('ALTER TABLE custos_fixos ADD COLUMN ativo BOOLEAN DEFAULT 1'))

    db.session.commit()

# ============== AUTENTICAÇÃO ==============

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

@app.route('/')
def index():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    return render_template('login.html')


@app.route('/healthz')
def healthz():
    return jsonify({'status': 'ok'}), 200

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        user = User.query.filter_by(email=email).first()
        
        if user and check_password_hash(user.password_hash, password):
            login_user(user)
            return redirect(url_for('dashboard'))
        flash('Email ou senha inválidos', 'error')
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        nome = request.form.get('nome')
        email = request.form.get('email')
        password = request.form.get('password')
        
        if User.query.filter_by(email=email).first():
            flash('Email já cadastrado', 'error')
            return render_template('register.html')
        
        user = User(nome=nome, email=email, password_hash=generate_password_hash(password))
        db.session.add(user)
        db.session.commit()
        flash('Cadastro realizado com sucesso!', 'success')
        return redirect(url_for('login'))
    return render_template('register.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

# ============== PÁGINAS PRINCIPAIS ==============

@app.route('/dashboard')
@login_required
def dashboard():
    hoje = date.today()
    inicio_mes = hoje.replace(day=1)

    vendas_mes = Venda.query.filter(
        Venda.user_id == current_user.id,
        Venda.data_venda >= inicio_mes
    ).all()

    insumos = Insumo.query.filter_by(user_id=current_user.id).all()
    produtos_ativos = Produto.query.filter_by(user_id=current_user.id, ativo=True).all()
    vendas_usuario = Venda.query.filter_by(user_id=current_user.id).all()
    total_faturamento = sum(v.valor_total for v in vendas_mes)
    total_custo = sum(v.custo_total or 0 for v in vendas_mes)
    total_lucro = sum(v.lucro_total or 0 for v in vendas_mes)
    insumos_baixo = [insumo for insumo in insumos if (insumo.estoque_atual or 0) <= (insumo.estoque_minimo or 0)]

    lucro_por_produto = {}
    for venda in vendas_mes:
        nome_produto = venda.produto.nome if venda.produto else 'Produto removido'
        if nome_produto not in lucro_por_produto:
            lucro_por_produto[nome_produto] = {
                'produto': nome_produto,
                'quantidade_vendida': 0,
                'faturamento': 0,
                'lucro': 0
            }
        lucro_por_produto[nome_produto]['quantidade_vendida'] += venda.quantidade or 0
        lucro_por_produto[nome_produto]['faturamento'] += venda.valor_total or 0
        lucro_por_produto[nome_produto]['lucro'] += venda.lucro_total or 0

    top_produtos_lucro = sorted(lucro_por_produto.values(), key=lambda item: item['lucro'], reverse=True)[:5]
    produtos_margem_alerta = [produto for produto in produtos_ativos if produto.margem_caiu]

    comparacao_por_insumo = {}
    for insumo in insumos:
        chave = insumo.nome.strip().lower()
        if insumo.compras:
            ultimas_por_fornecedor = {}
            compras_ordenadas = sorted(
                insumo.compras,
                key=lambda compra: (compra.data_compra or date.min, compra.id or 0),
                reverse=True
            )
            for compra in compras_ordenadas:
                fornecedor_chave = compra.fornecedor_nome_exibicao
                if fornecedor_chave not in ultimas_por_fornecedor:
                    ultimas_por_fornecedor[fornecedor_chave] = {
                        'nome': insumo.nome,
                        'fornecedor': fornecedor_chave,
                        'preco_compra': compra.preco_compra,
                        'quantidade_compra': compra.quantidade_compra,
                        'unidade_compra': compra.unidade_compra,
                        'custo_unitario': compra.custo_unitario_base
                    }
            comparacao_por_insumo.setdefault(chave, []).extend(ultimas_por_fornecedor.values())
        else:
            comparacao_por_insumo.setdefault(chave, []).append({
                'nome': insumo.nome,
                'fornecedor': insumo.fornecedor_nome_exibicao,
                'preco_compra': insumo.preco_compra,
                'quantidade_compra': insumo.quantidade_compra,
                'unidade_compra': insumo.unidade_compra,
                'custo_unitario': insumo.custo_unitario_base
            })

    comparacao_fornecedores = []
    for ofertas in comparacao_por_insumo.values():
        ofertas.sort(key=lambda item: item['custo_unitario'])
        melhor = ofertas[0]['custo_unitario'] if ofertas else 0
        for oferta in ofertas:
            oferta['melhor_preco'] = oferta['custo_unitario'] == melhor
            comparacao_fornecedores.append(oferta)

    comparacao_fornecedores.sort(key=lambda item: (item['nome'].lower(), item['custo_unitario']))

    chart_labels = []
    chart_faturamento = []
    chart_lucro = []
    for offset in range(5, -1, -1):
        mes = (inicio_mes.month - offset - 1) % 12 + 1
        ano = inicio_mes.year + ((inicio_mes.month - offset - 1) // 12)
        vendas_periodo = [
            venda for venda in vendas_usuario
            if venda.data_venda and venda.data_venda.month == mes and venda.data_venda.year == ano
        ]
        chart_labels.append(f'{mes:02d}/{str(ano)[-2:]}')
        chart_faturamento.append(round(sum(v.valor_total or 0 for v in vendas_periodo), 2))
        chart_lucro.append(round(sum(v.lucro_total or 0 for v in vendas_periodo), 2))

    stats = {
        'total_insumos': len(insumos),
        'total_receitas': Receita.query.filter_by(user_id=current_user.id).count(),
        'total_produtos': len(produtos_ativos),
        'insumos_baixo': len(insumos_baixo),
        'faturamento_mes': total_faturamento,
        'custo_mes': total_custo,
        'lucro_mes': total_lucro,
        'total_vendas_mes': len(vendas_mes),
        'margem_mes': (total_lucro / total_faturamento * 100) if total_faturamento > 0 else 0
    }

    return render_template(
        'dashboard.html',
        stats=stats,
        alertas_estoque=sorted(insumos_baixo, key=lambda item: ((item.estoque_atual or 0) - (item.estoque_minimo or 0))),
        top_produtos_lucro=top_produtos_lucro,
        comparacao_fornecedores=comparacao_fornecedores,
        chart_labels=chart_labels,
        chart_faturamento=chart_faturamento,
        chart_lucro=chart_lucro,
        produtos_margem_alerta=produtos_margem_alerta
    )

@app.route('/insumos')
@login_required
def page_insumos():
    return render_template('insumos.html')

@app.route('/insumos/relatorio')
@login_required
def page_insumos_relatorio():
    return render_template('insumos_relatorio.html')

@app.route('/receitas')
@login_required
def page_receitas():
    return render_template('receitas.html')

@app.route('/receitas/nova')
@login_required
def page_receita_nova():
    return render_template('receita_form.html')

@app.route('/receitas/<int:id>/editar')
@login_required
def page_receita_editar(id):
    return render_template('receita_form.html', receita_id=id)

@app.route('/produtos')
@login_required
def page_produtos():
    return render_template('produtos.html')

@app.route('/produtos/novo')
@login_required
def page_produto_novo():
    return render_template('produto_form.html')

@app.route('/produtos/<int:id>/editar')
@login_required
def page_produto_editar(id):
    return render_template('produto_form.html', produto_id=id)

@app.route('/fornecedores')
@login_required
def page_fornecedores():
    return render_template('fornecedores.html')

@app.route('/custos-fixos')
@login_required
def page_custos_fixos():
    return render_template('custos_fixos.html')

@app.route('/vendas')
@login_required
def page_vendas():
    return render_template('vendas.html')

@app.route('/vendas/nova')
@login_required
def page_venda_nova():
    return render_template('vendas_form.html')

@app.route('/vendas/<int:id>/editar')
@login_required
def page_venda_editar(id):
    return render_template('vendas_form.html', venda_id=id)

@app.route('/vendas/relatorio')
@login_required
def page_vendas_relatorio():
    return render_template('vendas_relatorio.html')

@app.route('/simulador')
@login_required
def page_simulador():
    return render_template('simulador.html')

# ============== API - FORNECEDORES ==============

@app.route('/api/fornecedores', methods=['GET', 'POST'])
@login_required
def api_fornecedores():
    if request.method == 'GET':
        fornecedores = Fornecedor.query.filter_by(user_id=current_user.id).order_by(Fornecedor.nome).all()
        return jsonify([{
            'id': f.id, 'nome': f.nome, 'cnpj': f.cnpj, 'telefone': f.telefone,
            'email': f.email, 'cidade': f.cidade, 'estado': f.estado, 'contato': f.contato
        } for f in fornecedores])
    
    data = request.get_json()
    fornecedor = Fornecedor(
        user_id=current_user.id,
        nome=data.get('nome'),
        cnpj=data.get('cnpj'),
        telefone=data.get('telefone'),
        email=data.get('email'),
        endereco=data.get('endereco'),
        cidade=data.get('cidade'),
        estado=data.get('estado'),
        cep=data.get('cep'),
        contato=data.get('contato'),
        observacoes=data.get('observacoes')
    )
    db.session.add(fornecedor)
    db.session.commit()
    return jsonify({'success': True, 'id': fornecedor.id})

@app.route('/api/fornecedores/<int:id>', methods=['GET', 'PUT', 'DELETE'])
@login_required
def api_fornecedor_detail(id):
    fornecedor = Fornecedor.query.filter_by(id=id, user_id=current_user.id).first_or_404()

    if request.method == 'GET':
        return jsonify({
            'id': fornecedor.id,
            'nome': fornecedor.nome,
            'cnpj': fornecedor.cnpj,
            'telefone': fornecedor.telefone,
            'email': fornecedor.email,
            'endereco': fornecedor.endereco,
            'cidade': fornecedor.cidade,
            'estado': fornecedor.estado,
            'cep': fornecedor.cep,
            'contato': fornecedor.contato,
            'observacoes': fornecedor.observacoes
        })
    
    if request.method == 'PUT':
        data = request.get_json()
        fornecedor.nome = data.get('nome', fornecedor.nome)
        fornecedor.cnpj = data.get('cnpj', fornecedor.cnpj)
        fornecedor.telefone = data.get('telefone', fornecedor.telefone)
        fornecedor.email = data.get('email', fornecedor.email)
        fornecedor.endereco = data.get('endereco', fornecedor.endereco)
        fornecedor.cidade = data.get('cidade', fornecedor.cidade)
        fornecedor.estado = data.get('estado', fornecedor.estado)
        fornecedor.cep = data.get('cep', fornecedor.cep)
        fornecedor.contato = data.get('contato', fornecedor.contato)
        fornecedor.observacoes = data.get('observacoes', fornecedor.observacoes)
        db.session.commit()
        return jsonify({'success': True})
    
    db.session.delete(fornecedor)
    db.session.commit()
    return jsonify({'success': True})

# ============== API - INSUMOS (ATUALIZADO) ==============

@app.route('/api/insumos', methods=['GET', 'POST'])
@login_required
def api_insumos():
    if request.method == 'GET':
        insumos = Insumo.query.filter_by(user_id=current_user.id).all()
        return jsonify([{
            'id': i.id, 'nome': i.nome, 'categoria': i.categoria,
            'unidade_compra': i.ultima_compra.unidade_compra if i.ultima_compra else i.unidade_compra,
            'quantidade_compra': i.ultima_compra.quantidade_compra if i.ultima_compra else i.quantidade_compra,
            'preco_compra': i.ultima_compra.preco_compra if i.ultima_compra else i.preco_compra,
            'unidade_estoque': i.unidade_compra,
            'custo_unitario_base': i.custo_unitario_base,
            'custo_unitario_medio': i.custo_unitario_medio,
            'custo_unitario_ultimo': i.custo_unitario_ultimo,
            'melhor_preco_unitario': i.melhor_preco_unitario,
            'estoque_atual': i.estoque_atual, 'estoque_minimo': i.estoque_minimo,
            'fornecedor_id': i.fornecedor_id,
            'fornecedor_nome': i.fornecedor_nome_exibicao,
            'compras_count': len(i.compras),
            'ultima_compra_data': i.ultima_compra.data_compra.isoformat() if i.ultima_compra and i.ultima_compra.data_compra else None
        } for i in insumos])
    
    data = request.get_json()
    insumo = Insumo(
        user_id=current_user.id,
        fornecedor_id=data.get('fornecedor_id') or None,
        fornecedor_nome_manual=data.get('fornecedor') or None,
        nome=data['nome'],
        categoria=data.get('categoria', 'ingrediente'),
        unidade_compra=data['unidade_compra'],
        quantidade_compra=float(data['quantidade_compra']),
        preco_compra=float(data['preco_compra']),
        estoque_atual=float(data.get('estoque_atual', 0)),
        estoque_minimo=float(data.get('estoque_minimo', 0))
    )
    db.session.add(insumo)
    db.session.flush()

    compra_inicial = CompraInsumo(
        user_id=current_user.id,
        insumo_id=insumo.id,
        fornecedor_id=data.get('fornecedor_id') or None,
        fornecedor_nome=data.get('fornecedor') or None,
        quantidade_compra=float(data['quantidade_compra']),
        unidade_compra=data['unidade_compra'],
        preco_compra=float(data['preco_compra']),
        impacta_estoque=False,
        data_compra=date.today(),
        observacoes='Compra inicial cadastrada junto com o insumo.'
    )
    db.session.add(compra_inicial)
    db.session.commit()
    return jsonify({'success': True, 'id': insumo.id})

@app.route('/api/insumos/<int:id>', methods=['GET', 'PUT', 'DELETE'])
@login_required
def api_insumo_detail(id):
    insumo = Insumo.query.filter_by(id=id, user_id=current_user.id).first_or_404()
    
    if request.method == 'GET':
        return jsonify({
            'id': insumo.id, 'nome': insumo.nome, 'categoria': insumo.categoria,
            'unidade_compra': insumo.ultima_compra.unidade_compra if insumo.ultima_compra else insumo.unidade_compra,
            'quantidade_compra': insumo.ultima_compra.quantidade_compra if insumo.ultima_compra else insumo.quantidade_compra,
            'preco_compra': insumo.ultima_compra.preco_compra if insumo.ultima_compra else insumo.preco_compra,
            'unidade_estoque': insumo.unidade_compra,
            'estoque_atual': insumo.estoque_atual,
            'estoque_minimo': insumo.estoque_minimo,
            'fornecedor_id': insumo.fornecedor_id,
            'fornecedor_nome': insumo.fornecedor_nome_exibicao,
            'fornecedor': insumo.fornecedor_nome_manual,
            'custo_unitario_medio': insumo.custo_unitario_medio,
            'custo_unitario_ultimo': insumo.custo_unitario_ultimo,
            'melhor_preco_unitario': insumo.melhor_preco_unitario
        })
    
    if request.method == 'PUT':
        data = request.get_json()
        insumo.nome = data.get('nome', insumo.nome)
        insumo.categoria = data.get('categoria', insumo.categoria)
        insumo.fornecedor_id = data.get('fornecedor_id') or None
        insumo.fornecedor_nome_manual = data.get('fornecedor', insumo.fornecedor_nome_manual)
        insumo.unidade_compra = data.get('unidade_compra', insumo.unidade_compra)
        insumo.quantidade_compra = float(data.get('quantidade_compra', insumo.quantidade_compra))
        insumo.preco_compra = float(data.get('preco_compra', insumo.preco_compra))
        insumo.estoque_atual = float(data.get('estoque_atual', insumo.estoque_atual))
        insumo.estoque_minimo = float(data.get('estoque_minimo', insumo.estoque_minimo))
        db.session.commit()
        return jsonify({'success': True})
    
    db.session.delete(insumo)
    db.session.commit()
    return jsonify({'success': True})

@app.route('/api/insumos/relatorio')
@login_required
def api_insumos_relatorio():
    insumos = Insumo.query.filter_by(user_id=current_user.id).all()
    
    estoque_ok = []
    estoque_baixo = []
    
    for i in insumos:
        item = {
            'id': i.id, 'nome': i.nome, 'estoque_atual': i.estoque_atual,
            'estoque_minimo': i.estoque_minimo, 'unidade': i.unidade_compra,
            'custo_unitario': i.custo_unitario_base,
            'fornecedor': i.fornecedor_nome_exibicao,
            'ultimo_preco_unitario': i.custo_unitario_ultimo,
            'melhor_preco_unitario': i.melhor_preco_unitario
        }
        if i.estoque_atual <= i.estoque_minimo:
            estoque_baixo.append(item)
        else:
            estoque_ok.append(item)
    
    return jsonify({
        'estoque_ok': estoque_ok,
        'estoque_baixo': estoque_baixo,
        'total_insumos': len(insumos),
        'total_baixo': len(estoque_baixo)
    })


@app.route('/api/insumos/<int:id>/compras', methods=['GET', 'POST'])
@login_required
def api_insumo_compras(id):
    insumo = Insumo.query.filter_by(id=id, user_id=current_user.id).first_or_404()

    if request.method == 'GET':
        compras = CompraInsumo.query.filter_by(insumo_id=id, user_id=current_user.id).order_by(CompraInsumo.data_compra.desc(), CompraInsumo.id.desc()).all()
        return jsonify([{
            'id': compra.id,
            'fornecedor_id': compra.fornecedor_id,
            'fornecedor_nome': compra.fornecedor_nome_exibicao,
            'quantidade_compra': compra.quantidade_compra,
            'unidade_compra': compra.unidade_compra,
            'preco_compra': compra.preco_compra,
            'custo_unitario_base': compra.custo_unitario_base,
            'impacta_estoque': compra.impacta_estoque,
            'data_compra': compra.data_compra.isoformat() if compra.data_compra else None,
            'observacoes': compra.observacoes
        } for compra in compras])

    data = request.get_json()
    compra = CompraInsumo(
        user_id=current_user.id,
        insumo_id=id,
        fornecedor_id=data.get('fornecedor_id') or None,
        fornecedor_nome=data.get('fornecedor_nome') or None,
        quantidade_compra=float(data['quantidade_compra']),
        unidade_compra=data.get('unidade_compra', insumo.unidade_compra),
        preco_compra=float(data['preco_compra']),
        impacta_estoque=bool(data.get('impacta_estoque', True)),
        data_compra=datetime.strptime(data['data_compra'], '%Y-%m-%d').date() if data.get('data_compra') else date.today(),
        observacoes=data.get('observacoes')
    )
    db.session.add(compra)
    db.session.flush()

    if compra.impacta_estoque:
        aplicar_compra_no_estoque(insumo, compra.quantidade_compra, compra.unidade_compra)

    sincronizar_insumo_com_ultima_compra(insumo)
    db.session.commit()
    return jsonify({'success': True, 'id': compra.id})


@app.route('/api/insumos/<int:insumo_id>/compras/<int:compra_id>', methods=['PUT', 'DELETE'])
@login_required
def api_insumo_compra_detail(insumo_id, compra_id):
    insumo = Insumo.query.filter_by(id=insumo_id, user_id=current_user.id).first_or_404()
    compra = CompraInsumo.query.filter_by(id=compra_id, insumo_id=insumo_id, user_id=current_user.id).first_or_404()

    if compra.impacta_estoque:
        aplicar_compra_no_estoque(insumo, compra.quantidade_compra, compra.unidade_compra, remover=True)

    if request.method == 'PUT':
        data = request.get_json()
        compra.fornecedor_id = data.get('fornecedor_id') or None
        compra.fornecedor_nome = data.get('fornecedor_nome') or None
        compra.quantidade_compra = float(data.get('quantidade_compra', compra.quantidade_compra))
        compra.unidade_compra = data.get('unidade_compra', compra.unidade_compra)
        compra.preco_compra = float(data.get('preco_compra', compra.preco_compra))
        compra.impacta_estoque = bool(data.get('impacta_estoque', compra.impacta_estoque))
        compra.data_compra = datetime.strptime(data['data_compra'], '%Y-%m-%d').date() if data.get('data_compra') else compra.data_compra
        compra.observacoes = data.get('observacoes', compra.observacoes)

        if compra.impacta_estoque:
            aplicar_compra_no_estoque(insumo, compra.quantidade_compra, compra.unidade_compra)

        db.session.flush()
        sincronizar_insumo_com_ultima_compra(insumo)
        db.session.commit()
        return jsonify({'success': True})

    db.session.delete(compra)
    db.session.flush()
    sincronizar_insumo_com_ultima_compra(insumo)
    db.session.commit()
    return jsonify({'success': True})

# ============== API - RECEITAS (ATUALIZADO COM DELETE) ==============

@app.route('/api/receitas', methods=['GET', 'POST'])
@login_required
def api_receitas():
    if request.method == 'GET':
        receitas = Receita.query.filter_by(user_id=current_user.id).all()
        return jsonify([{
            'id': r.id, 'nome': r.nome, 'categoria': r.categoria,
            'rendimento_teorico': r.rendimento_teorico, 'perda_percentual': r.perda_percentual,
            'rendimento_real': r.rendimento_real, 'custo_total': r.custo_total,
            'custo_unitario': r.custo_unitario, 'quantidade_insumos': len(r.insumos),
            'insumos_alerta_preco': sum(1 for insumo in r.insumos if insumo.compras and insumo.custo_unitario_ultimo > insumo.custo_unitario_medio)
        } for r in receitas])
    
    data = request.get_json()
    receita = Receita(
        user_id=current_user.id, nome=data['nome'], categoria=data.get('categoria'),
        rendimento_teorico=int(data.get('rendimento_teorico', 1)),
        perda_percentual=float(data.get('perda_percentual', 0)),
        tempo_preparo=int(data.get('tempo_preparo', 0) or 0)
    )
    db.session.add(receita)
    db.session.commit()
    return jsonify({'success': True, 'id': receita.id})

@app.route('/api/receitas/<int:id>', methods=['GET', 'PUT', 'DELETE'])
@login_required
def api_receita_detail(id):
    receita = Receita.query.filter_by(id=id, user_id=current_user.id).first_or_404()
    
    if request.method == 'GET':
        insumos_data = []
        for insumo in receita.insumos:
            assoc = db.session.execute(
                receita_insumos.select().where(
                    receita_insumos.c.receita_id == receita.id,
                    receita_insumos.c.insumo_id == insumo.id
                )
            ).fetchone()
            if assoc:
                custo_item = insumo.get_custo_por_unidade(assoc.unidade) * assoc.quantidade
                insumos_data.append({
                    'id': insumo.id, 'nome': insumo.nome, 'quantidade': assoc.quantidade,
                    'unidade': assoc.unidade, 'custo_unitario': insumo.get_custo_por_unidade(assoc.unidade),
                    'custo_total': custo_item
                })
        
        return jsonify({
            'id': receita.id, 'nome': receita.nome, 'categoria': receita.categoria,
            'rendimento_teorico': receita.rendimento_teorico, 'perda_percentual': receita.perda_percentual,
            'tempo_preparo': receita.tempo_preparo,
            'rendimento_real': receita.rendimento_real, 'custo_total': receita.custo_total,
            'custo_unitario': receita.custo_unitario, 'insumos': insumos_data
        })
    
    if request.method == 'PUT':
        data = request.get_json()
        receita.nome = data.get('nome', receita.nome)
        receita.categoria = data.get('categoria', receita.categoria)
        receita.rendimento_teorico = int(data.get('rendimento_teorico', receita.rendimento_teorico))
        receita.perda_percentual = float(data.get('perda_percentual', receita.perda_percentual))
        receita.tempo_preparo = int(data.get('tempo_preparo', receita.tempo_preparo or 0) or 0)
        db.session.commit()
        return jsonify({'success': True})
    
    # DELETE - Verificar se há produtos vinculados
    produtos_vinculados = Produto.query.filter_by(receita_id=id, ativo=True).count()
    if produtos_vinculados > 0:
        return jsonify({'error': f'Não é possível excluir. Existem {produtos_vinculados} produtos usando esta receita.'}), 400
    
    db.session.delete(receita)
    db.session.commit()
    return jsonify({'success': True})

@app.route('/api/receitas/<int:id>/insumos', methods=['POST'])
@login_required
def api_receita_add_insumo(id):
    receita = Receita.query.filter_by(id=id, user_id=current_user.id).first_or_404()
    data = request.get_json()
    
    existing = db.session.execute(
        receita_insumos.select().where(
            receita_insumos.c.receita_id == id,
            receita_insumos.c.insumo_id == data['insumo_id']
        )
    ).fetchone()
    
    if existing:
        db.session.execute(
            receita_insumos.update().where(
                receita_insumos.c.receita_id == id,
                receita_insumos.c.insumo_id == data['insumo_id']
            ).values(quantidade=float(data['quantidade']), unidade=data['unidade'])
        )
    else:
        db.session.execute(
            receita_insumos.insert().values(
                receita_id=id, insumo_id=data['insumo_id'],
                quantidade=float(data['quantidade']), unidade=data['unidade']
            )
        )
    
    db.session.commit()
    return jsonify({'success': True})

@app.route('/api/receitas/<int:receita_id>/insumos/<int:insumo_id>', methods=['DELETE'])
@login_required
def api_receita_remove_insumo(receita_id, insumo_id):
    receita = Receita.query.filter_by(id=receita_id, user_id=current_user.id).first_or_404()
    db.session.execute(
        receita_insumos.delete().where(
            receita_insumos.c.receita_id == receita_id,
            receita_insumos.c.insumo_id == insumo_id
        )
    )
    db.session.commit()
    return jsonify({'success': True})

# ============== API - PRODUTOS (ATUALIZADO COM EDIT/DELETE) ==============

@app.route('/api/produtos', methods=['GET', 'POST'])
@login_required
def api_produtos():
    if request.method == 'GET':
        produtos = Produto.query.filter_by(user_id=current_user.id, ativo=True).all()
        return jsonify([{
            'id': p.id, 'nome': p.nome, 'receita_id': p.receita_id,
            'receita_nome': p.receita.nome if p.receita else None,
            'custo_real': p.custo_real_atual,
            'custo_cadastrado': p.custo_real,
            'margem_lucro': p.margem_lucro,
            'preco_venda': p.preco_venda or p.calcular_preco(),
            'lucro_unitario': p.lucro_unitario_atual,
            'margem_real': p.margem_real_atual,
            'margem_caiu': p.margem_caiu,
            'preco_sugerido_atual': p.calcular_preco_com_custo(p.custo_real_atual)
        } for p in produtos])
    
    data = request.get_json()
    receita = Receita.query.filter_by(id=data['receita_id'], user_id=current_user.id).first_or_404()
    
    produto = Produto(
        user_id=current_user.id, receita_id=receita.id,
        nome=data.get('nome', receita.nome), custo_real=receita.custo_unitario,
        margem_lucro=float(data.get('margem_lucro', 30)),
        markup=float(data['markup']) if data.get('markup') else None,
        taxa_venda=float(data.get('taxa_venda', 0))
    )
    produto.preco_venda = produto.calcular_preco()
    
    db.session.add(produto)
    db.session.commit()
    return jsonify({'success': True, 'id': produto.id, 'preco_venda': produto.preco_venda})

@app.route('/api/produtos/<int:id>', methods=['GET', 'PUT', 'DELETE'])
@login_required
def api_produto_detail(id):
    produto = Produto.query.filter_by(id=id, user_id=current_user.id).first_or_404()
    
    if request.method == 'GET':
        return jsonify({
            'id': produto.id, 'nome': produto.nome, 'receita_id': produto.receita_id,
            'custo_real': produto.custo_real, 'margem_lucro': produto.margem_lucro,
            'markup': produto.markup, 'taxa_venda': produto.taxa_venda,
            'preco_venda': produto.preco_venda,
            'custo_real_atual': produto.custo_real_atual,
            'margem_real_atual': produto.margem_real_atual,
            'margem_caiu': produto.margem_caiu
        })
    
    if request.method == 'PUT':
        data = request.get_json()
        if data.get('receita_id'):
            receita = Receita.query.filter_by(id=data['receita_id'], user_id=current_user.id).first_or_404()
            produto.receita_id = receita.id
            produto.custo_real = receita.custo_unitario
        produto.nome = data.get('nome', produto.nome)
        produto.margem_lucro = float(data.get('margem_lucro', produto.margem_lucro))
        produto.markup = float(data['markup']) if data.get('markup') else None
        produto.taxa_venda = float(data.get('taxa_venda', produto.taxa_venda))
        produto.preco_venda = produto.calcular_preco()
        db.session.commit()
        return jsonify({'success': True, 'preco_venda': produto.preco_venda})
    
    # Soft delete
    produto.ativo = False
    db.session.commit()
    return jsonify({'success': True})

# ============== API - VENDAS ==============

@app.route('/api/vendas', methods=['GET', 'POST'])
@login_required
def api_vendas():
    if request.method == 'GET':
        vendas = Venda.query.filter_by(user_id=current_user.id).order_by(Venda.data_venda.desc()).all()
        return jsonify([{
            'id': v.id, 'produto_nome': v.produto.nome, 'quantidade': v.quantidade,
            'preco_unitario': v.preco_unitario, 'valor_total': v.valor_total,
            'forma_pagamento': v.forma_pagamento, 'tem_entrega': v.tem_entrega,
            'valor_entrega': v.valor_entrega, 'custo_total': v.custo_total,
            'lucro_total': v.lucro_total, 'data_venda': v.data_venda.isoformat()
        } for v in vendas])
    
    data = request.get_json()
    produto = Produto.query.filter_by(id=data['produto_id'], user_id=current_user.id).first_or_404()
    
    quantidade = int(data.get('quantidade', 1))
    preco_unitario = float(data.get('preco_unitario', produto.preco_venda or produto.calcular_preco()))
    tem_entrega = data.get('tem_entrega', False)
    valor_entrega = float(data.get('valor_entrega', 0)) if tem_entrega else 0
    custo_atual = produto.custo_real_atual
    
    valor_total = (preco_unitario * quantidade) + valor_entrega
    custo_total = custo_atual * quantidade
    lucro_total = (preco_unitario - custo_atual) * quantidade
    
    venda = Venda(
        user_id=current_user.id,
        produto_id=produto.id,
        quantidade=quantidade,
        preco_unitario=preco_unitario,
        valor_total=valor_total,
        forma_pagamento=data.get('forma_pagamento'),
        tem_entrega=tem_entrega,
        valor_entrega=valor_entrega,
        custo_total=custo_total,
        lucro_total=lucro_total,
        observacoes=data.get('observacoes')
    )

    aplicar_movimento_estoque(calcular_baixa_estoque(produto, quantidade))
    db.session.add(venda)
    db.session.commit()
    return jsonify({'success': True, 'id': venda.id})


@app.route('/api/vendas/<int:id>', methods=['GET', 'PUT', 'DELETE'])
@login_required
def api_venda_detail(id):
    venda = Venda.query.filter_by(id=id, user_id=current_user.id).first_or_404()

    if request.method == 'GET':
        return jsonify({
            'id': venda.id,
            'produto_id': venda.produto_id,
            'quantidade': venda.quantidade,
            'preco_unitario': venda.preco_unitario,
            'forma_pagamento': venda.forma_pagamento,
            'tem_entrega': venda.tem_entrega,
            'valor_entrega': venda.valor_entrega,
            'observacoes': venda.observacoes,
            'data_venda': venda.data_venda.isoformat() if venda.data_venda else None
        })

    aplicar_movimento_estoque(calcular_baixa_estoque(venda.produto, venda.quantidade), restaurar=True)

    if request.method == 'PUT':
        data = request.get_json()
        produto = Produto.query.filter_by(id=data['produto_id'], user_id=current_user.id).first_or_404()

        venda.produto_id = produto.id
        venda.quantidade = int(data.get('quantidade', venda.quantidade))
        venda.preco_unitario = float(data.get('preco_unitario', produto.preco_venda or produto.calcular_preco()))
        venda.forma_pagamento = data.get('forma_pagamento', venda.forma_pagamento)
        venda.tem_entrega = data.get('tem_entrega', venda.tem_entrega)
        venda.valor_entrega = float(data.get('valor_entrega', venda.valor_entrega or 0)) if venda.tem_entrega else 0
        venda.valor_total = (venda.preco_unitario * venda.quantidade) + venda.valor_entrega
        custo_atual = produto.custo_real_atual
        venda.custo_total = custo_atual * venda.quantidade
        venda.lucro_total = (venda.preco_unitario - custo_atual) * venda.quantidade
        venda.observacoes = data.get('observacoes', venda.observacoes)

        aplicar_movimento_estoque(calcular_baixa_estoque(produto, venda.quantidade))
        db.session.commit()
        return jsonify({'success': True})

    db.session.delete(venda)
    db.session.commit()
    return jsonify({'success': True})

@app.route('/api/vendas/relatorio')
@login_required
def api_vendas_relatorio():
    from_date = request.args.get('from', date.today().replace(day=1).isoformat())
    to_date = request.args.get('to', date.today().isoformat())
    
    vendas = Venda.query.filter(
        Venda.user_id == current_user.id,
        Venda.data_venda >= from_date,
        Venda.data_venda <= to_date
    ).all()
    
    total_faturamento = sum(v.valor_total for v in vendas)
    total_custo = sum(v.custo_total or 0 for v in vendas)
    total_lucro = sum(v.lucro_total or 0 for v in vendas)
    total_entregas = sum(v.valor_entrega or 0 for v in vendas)
    
    # Por forma de pagamento
    por_pagamento = {}
    for v in vendas:
        fp = v.forma_pagamento or 'Não informado'
        if fp not in por_pagamento:
            por_pagamento[fp] = {'quantidade': 0, 'valor': 0}
        por_pagamento[fp]['quantidade'] += 1
        por_pagamento[fp]['valor'] += v.valor_total
    
    return jsonify({
        'periodo': {'de': from_date, 'ate': to_date},
        'resumo': {
            'total_vendas': len(vendas),
            'faturamento': total_faturamento,
            'custo_total': total_custo,
            'lucro_total': total_lucro,
            'margem_lucro': (total_lucro / total_faturamento * 100) if total_faturamento > 0 else 0,
            'total_entregas': total_entregas
        },
        'por_pagamento': por_pagamento,
        'vendas': [{
            'id': v.id, 'produto': v.produto.nome, 'data': v.data_venda.isoformat(),
            'valor': v.valor_total, 'lucro': v.lucro_total
        } for v in vendas]
    })

# ============== API - SIMULADOR ==============

@app.route('/api/simular', methods=['POST'])
@login_required
def api_simular():
    data = request.get_json()
    custo_real = float(data['custo_real'])
    taxa_venda = to_float(data.get('taxa_venda'))
    
    if data.get('preco_venda'):
        preco = float(data['preco_venda'])
        if taxa_venda > 0:
            preco = preco / (1 - taxa_venda / 100)
        lucro = preco - custo_real
        margem = (lucro / preco * 100) if preco > 0 else 0
        markup = preco / custo_real if custo_real > 0 else 0
        return jsonify({'preco_venda': preco, 'lucro_unitario': lucro, 'margem_real': margem, 'markup': markup})
    
    elif data.get('margem'):
        margem_decimal = float(data['margem']) / 100
        if margem_decimal >= 1:
            margem_decimal = 0.99
        preco = custo_real / (1 - margem_decimal)
        if taxa_venda > 0:
            preco = preco / (1 - taxa_venda / 100)
        return jsonify({
            'preco_venda': round(preco, 2),
            'lucro_unitario': round(preco - custo_real, 2),
            'margem_real': float(data['margem']),
            'markup': round(preco / custo_real, 2) if custo_real > 0 else 0
        })
    
    elif data.get('markup'):
        preco = custo_real * float(data['markup'])
        if taxa_venda > 0:
            preco = preco / (1 - taxa_venda / 100)
        lucro = preco - custo_real
        margem = (lucro / preco * 100) if preco > 0 else 0
        return jsonify({'preco_venda': round(preco, 2), 'lucro_unitario': round(lucro, 2), 'margem_real': round(margem, 2), 'markup': float(data['markup'])})
    
    return jsonify({'error': 'Parâmetros inválidos'}), 400

# ============== INICIALIZAÇÃO ==============

@app.route('/api/custos_fixos', methods=['GET', 'POST'])
@login_required
def api_custos_fixos():
    if request.method == 'GET':
        custos = CustoFixo.query.filter_by(user_id=current_user.id, ativo=True).order_by(CustoFixo.descricao).all()
        return jsonify([{
            'id': custo.id,
            'descricao': custo.descricao,
            'valor_mensal': custo.valor_mensal,
            'categoria': custo.categoria,
            'dia_vencimento': custo.dia_vencimento
        } for custo in custos])

    data = request.get_json()
    custo = CustoFixo(
        user_id=current_user.id,
        descricao=data['descricao'],
        valor_mensal=to_float(data.get('valor_mensal')),
        categoria=data.get('categoria'),
        dia_vencimento=data.get('dia_vencimento')
    )
    db.session.add(custo)
    db.session.commit()
    return jsonify({'success': True, 'id': custo.id})


@app.route('/api/custos_fixos/<int:id>', methods=['GET', 'PUT', 'DELETE'])
@login_required
def api_custo_fixo_detail(id):
    custo = CustoFixo.query.filter_by(id=id, user_id=current_user.id).first_or_404()

    if request.method == 'GET':
        return jsonify({
            'id': custo.id,
            'descricao': custo.descricao,
            'valor_mensal': custo.valor_mensal,
            'categoria': custo.categoria,
            'dia_vencimento': custo.dia_vencimento
        })

    if request.method == 'PUT':
        data = request.get_json()
        custo.descricao = data.get('descricao', custo.descricao)
        custo.valor_mensal = to_float(data.get('valor_mensal'), custo.valor_mensal)
        custo.categoria = data.get('categoria', custo.categoria)
        custo.dia_vencimento = data.get('dia_vencimento', custo.dia_vencimento)
        db.session.commit()
        return jsonify({'success': True})

    custo.ativo = False
    db.session.commit()
    return jsonify({'success': True})


with app.app_context():
    db.create_all()
    ensure_sqlite_schema_compatibility()


if __name__ == '__main__':
    app.run(
        debug=os.getenv('FLASK_DEBUG', '0') == '1',
        host='0.0.0.0',
        port=int(os.getenv('PORT', '5000'))
    )
