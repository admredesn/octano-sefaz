// ================================================================ PAGAR COM CARTÃO DE CRÉDITO (20/10/2026)
// Ronan: o cliente paga uma compra a prazo ou uma fatura pelo app com cartão de crédito, parcelado com os juros por
// conta dele (repasse de taxa do PagBank). Telas do layout aprovado (_ferramentas/app_cartao_layout_teste.html):
//   formas (cartões salvos, outro cartão, Pix) -> cartão salvo: parcelas -> CVV | cartão novo: dados + parcelas
//   -> pagar -> aprovado. Pix continua o de sempre (pagarAlvo, faturas.js).
// Segurança: o número do cartão é criptografado AQUI pelo SDK do PagBank (chave pública que o servidor manda) e só o
// `encrypted` sai do celular; cada encrypted é gerado na hora de pagar. O servidor recalcula os juros no PagBank (o app
// só manda o número de parcelas) e dá a baixa sozinho. Rotas: cliente_cartao.py.
const PG = { alvo: null, cfg: null, forma: null, planos: null, parcelas: 1, bin: "", pagando: false, timer: null, res: null };
const PG_SDK = "https://assets.pagseguro.com.br/checkout-sdk-js/rc/dist/browser/pagseguro.min.js";

// chamado pelo botão Pagar (faturas.js): cartão ligado no posto = escolher a forma; senão o Pix de sempre
async function pagarEscolher(tipo, id, titulo, valor, posto) {
  let cfg = null;
  if (posto) {
    toast("Abrindo o pagamento…", 1200);
    const r = await api("/cashback/api/cartao/config?posto=" + encodeURIComponent(posto));
    if (r.ok && r.ativo) cfg = r;
  }
  if (!cfg) return pagarAlvo(tipo === "nota" ? { nota_id: id } : { fatura_id: id }, tipo === "nota" ? "esta compra" : "a fatura");
  pgParar();
  PG.alvo = { tipo, id, titulo, valor: Number(valor || 0), posto };
  PG.cfg = cfg;
  PG.forma = (cfg.salvos || []).length ? "salvo:" + cfg.salvos[0].id : "novo";
  PG.planos = null; PG.parcelas = 1; PG.bin = ""; PG.res = null;
  ir("#/pagar/formas");
}

function pgParar() { if (PG.timer) { clearInterval(PG.timer); PG.timer = null; } PG.pagando = false; }
function pgCorpo() { return PG.alvo && PG.alvo.tipo === "nota" ? { nota_id: PG.alvo.id } : { fatura_id: PG.alvo.id }; }
function pgSalvo() { return PG.forma && PG.forma.indexOf("salvo:") === 0 ? (PG.cfg.salvos || []).find(s => "salvo:" + s.id === PG.forma) : null; }
function pgBandeira(b) { const s = String(b || "").toLowerCase(); return s === "mastercard" ? "Mastercard" : s === "visa" ? "Visa" : s === "elo" ? "Elo" : s === "amex" ? "Amex" : s === "hipercard" ? "Hipercard" : (s ? s.charAt(0).toUpperCase() + s.slice(1) : "Cartão"); }
function pgPassos(i) { return `<div class="pg-passos">${[1, 2, 3].map(k => `<i class="${k <= i ? "on" : ""}"></i>`).join("")}</div>`; }
function pgResumo() {
  return `<div class="card pg-resumo"><div class="s">${esc(PG.alvo.titulo)}</div><div class="v">${brl(PG.alvo.valor)}</div></div>`;
}

async function telaPagar(etapa) {
  if (!PG.alvo || !PG.cfg) { ir("#/faturas"); return; }
  if (etapa === "formas") return pgTelaFormas();
  if (etapa === "parcelas") return pgTelaParcelas();
  if (etapa === "cvv") return pgTelaCvv();
  if (etapa === "cartao") return pgTelaCartao();
  if (etapa === "ok") return pgTelaOk();
  ir("#/pagar/formas");
}
TELAS.pagar = a => telaPagar(a);

// ---------------------------------------------------------------- 1. como pagar
function pgTelaFormas() {
  const salvos = PG.cfg.salvos || [];
  const op = (chave, ic, titulo, desc, extra) => `<button class="pg-op ${PG.forma === chave ? "sel" : ""}" onclick="PG.forma='${chave}';pgTelaFormas()">
      <span class="ic">${ic}</span><span class="txt"><b>${titulo}</b><span class="d">${desc}</span></span>${extra || ""}</button>`;
  el().innerHTML = `<div class="tela">${topoSimples("Pagar", true)}<div class="secao" style="padding-top:2px">
    ${pgPassos(1)}${pgResumo()}
    <div class="pg-rot">Como você quer pagar?</div>
    ${salvos.map(s => op("salvo:" + s.id, "💳", `${esc(pgBandeira(s.bandeira))} •••• ${esc(s.final)}`,
        `Cartão salvo · só o código de segurança${s.validade ? " · vence " + esc(s.validade) : ""}`)).join("")}
    ${op("novo", "➕", "Outro cartão de crédito", "Digitar os dados · pode salvar para a próxima")}
    ${op("pix", '<span style="color:var(--verde,#12804A);font-weight:800;font-size:.8rem">PIX</span>', "Pix", "À vista · baixa na hora")}
    ${salvos.length && pgSalvo() ? `<button class="link pg-remover" onclick="pgRemoverSalvo(${Number(pgSalvo().id)})">Remover este cartão salvo</button>` : ""}
    <button class="btn" style="margin-top:14px" onclick="pgContinuar()">Continuar</button>
    <p class="pg-nota">Parcelado em até ${Number(PG.cfg.max_parcelas || 6)}x com os juros do cartão por conta de quem paga (parcela mínima ${brl(PG.cfg.parcela_min || 50)}).</p>
  </div></div>`;
}

function pgContinuar() {
  if (PG.forma === "pix") { pagarAlvo(pgCorpo(), PG.alvo.tipo === "nota" ? "esta compra" : "a fatura"); return; }
  PG.planos = null; PG.parcelas = 1; PG.bin = "";
  ir(PG.forma === "novo" ? "#/pagar/cartao" : "#/pagar/parcelas");
}

async function pgRemoverSalvo(id) {
  if (!confirm("Remover este cartão do app? Para usar de novo, será preciso digitar os dados.")) return;
  const r = await api("/cashback/api/cartao/salvos/" + encodeURIComponent(id), null, "DELETE");
  if (r.ok === false || r.erro) { toast(r.erro || "Não consegui remover agora."); return; }
  PG.cfg.salvos = (PG.cfg.salvos || []).filter(s => s.id !== id);
  PG.forma = PG.cfg.salvos.length ? "salvo:" + PG.cfg.salvos[0].id : "novo";
  toast("Cartão removido.");
  pgTelaFormas();
}

// ---------------------------------------------------------------- parcelas (com os juros reais do PagBank)
async function pgCarregarPlanos(extra) {
  const r = await api("/cashback/api/cartao/parcelas", Object.assign(pgCorpo(), extra));
  if (!r.ok) return { erro: r.erro || "Não consegui calcular as parcelas agora." };
  PG.planos = r.planos || [];
  if (r.valor) PG.alvo.valor = Number(r.valor);
  if (!PG.planos.find(p => p.parcelas === PG.parcelas)) PG.parcelas = 1;
  return { ok: true };
}

function pgListaPlanos(onde) {
  return (PG.planos || []).map(p => `<button class="pg-parc ${PG.parcelas === p.parcelas ? "sel" : ""}" onclick="PG.parcelas=${Number(p.parcelas)};${onde}">
      <span><b>${p.parcelas}x</b> de <b>${brl(p.valor_parcela)}</b>${p.sem_juros || p.parcelas === 1 ? ' <span class="pg-tag">SEM JUROS</span>' : ""}</span>
      <span class="t">${p.parcelas === 1 ? "à vista" : "total " + brl(p.total)}</span></button>`).join("")
    + ((PG.planos || []).length && (PG.planos || []).length < Number(PG.cfg.max_parcelas || 6)
      ? `<p class="pg-nota">Parcela mínima de ${brl(PG.cfg.parcela_min || 50)}.</p>` : "");
}
function pgPlano() { return (PG.planos || []).find(p => p.parcelas === PG.parcelas) || null; }

async function pgTelaParcelas() {
  const s = pgSalvo();
  if (!s) { ir("#/pagar/formas"); return; }
  el().innerHTML = `<div class="tela">${topoSimples("Parcelas", true)}<div class="secao" style="padding-top:2px">${pgPassos(2)}${pgResumo()}
    <div class="pg-rot">Em quantas vezes? <span class="pg-nota" style="display:inline">(juros do cartão por conta de quem paga)</span></div>
    <div id="pg-planos">${PG.planos ? pgListaPlanos("pgTelaParcelas()") : '<div class="sk" style="height:150px"></div>'}</div>
    <button class="btn" style="margin-top:12px" ${PG.planos ? "" : "disabled"} onclick="ir('#/pagar/cvv')">Continuar</button>
    <div class="msg erro" id="pg-msg"></div></div></div>`;
  if (!PG.planos) {
    const r = await pgCarregarPlanos({ cartao_salvo_id: s.id });
    if (!fatAindaAqui("#/pagar/parcelas")) return;
    if (r.erro) { document.getElementById("pg-msg").textContent = r.erro; document.getElementById("pg-planos").innerHTML = ""; return; }
    pgTelaParcelas();
  }
}

// ---------------------------------------------------------------- 3a. cartão salvo: só o CVV
function pgTelaCvv() {
  const s = pgSalvo(), p = pgPlano();
  if (!s || !p) { ir("#/pagar/formas"); return; }
  el().innerHTML = `<div class="tela">${topoSimples("Cartão salvo", true)}<div class="secao" style="padding-top:2px">${pgPassos(3)}
    <div class="card" style="padding:14px 16px">
      <div class="pg-op sel" style="margin:0;cursor:default"><span class="ic">💳</span><span class="txt"><b>${esc(pgBandeira(s.bandeira))} •••• ${esc(s.final)}</b>
        <span class="d">${s.validade ? "vence " + esc(s.validade) : "cartão salvo"}</span></span></div>
      <div class="campo"><label for="pg-cvv">Código de segurança (CVV)</label><input id="pg-cvv" inputmode="numeric" maxlength="4" autocomplete="cc-csc" placeholder="123"></div>
    </div>
    <button class="btn" id="pg-btn" style="margin-top:12px" onclick="pgPagar()">Pagar ${p.parcelas}x de ${brl(p.valor_parcela)}</button>
    <div class="msg erro" id="pg-msg"></div>
    <p class="pg-nota">Total no cartão: ${brl(p.total)}.</p></div></div>`;
  const c = document.getElementById("pg-cvv");
  c.addEventListener("input", () => { c.value = soDigitos(c.value).slice(0, 4); });
  c.focus();
}

// ---------------------------------------------------------------- 3b. cartão novo: dados + parcelas
function pgTelaCartao() {
  el().innerHTML = `<div class="tela">${topoSimples("Cartão de crédito", true)}<div class="secao" style="padding-top:2px">${pgPassos(2)}${pgResumo()}
    <div class="card" style="padding:4px 16px 14px">
      <div class="campo"><label for="pg-num">Número do cartão</label><input id="pg-num" inputmode="numeric" autocomplete="cc-number" placeholder="0000 0000 0000 0000" maxlength="23"></div>
      <div class="duas">
        <div class="campo"><label for="pg-val">Validade</label><input id="pg-val" inputmode="numeric" autocomplete="cc-exp" placeholder="MM/AA" maxlength="5"></div>
        <div class="campo"><label for="pg-cvv">CVV</label><input id="pg-cvv" inputmode="numeric" autocomplete="cc-csc" placeholder="123" maxlength="4"></div>
      </div>
      <div class="campo"><label for="pg-nome">Nome impresso no cartão</label><input id="pg-nome" autocomplete="cc-name" placeholder="COMO NO CARTÃO" style="text-transform:uppercase"></div>
      <label class="check" style="margin-top:12px"><input type="checkbox" id="pg-salvar" checked> <span>Salvar este cartão para a próxima</span></label>
    </div>
    <div class="pg-rot" id="pg-rot-parc" style="display:none">Em quantas vezes? <span class="pg-nota" style="display:inline">(juros do cartão por conta de quem paga)</span></div>
    <div id="pg-planos"></div>
    <button class="btn" id="pg-btn" style="margin-top:12px" disabled onclick="pgPagar()">Digite o número do cartão</button>
    <div class="msg erro" id="pg-msg"></div>
    <p class="pg-nota">🔒 Os dados vão criptografados direto para o PagBank. O Posto SN nunca vê o número do cartão.</p>
  </div></div>`;
  const num = document.getElementById("pg-num"), val = document.getElementById("pg-val"), cvv = document.getElementById("pg-cvv");
  num.addEventListener("input", () => {
    const d = soDigitos(num.value).slice(0, 19);
    num.value = d.replace(/(\d{4})(?=\d)/g, "$1 ");
    const bin = d.slice(0, 6);
    if (d.length >= 6 && bin !== PG.bin) { PG.bin = bin; pgPlanosNovo(); }
    if (d.length < 6 && PG.bin) { PG.bin = ""; PG.planos = null; pgDesenharPlanosNovo(); }
  });
  val.addEventListener("input", () => {
    const d = soDigitos(val.value).slice(0, 4);
    val.value = d.length > 2 ? d.slice(0, 2) + "/" + d.slice(2) : d;
  });
  cvv.addEventListener("input", () => { cvv.value = soDigitos(cvv.value).slice(0, 4); });
  num.focus();
}

async function pgPlanosNovo() {
  const bin = PG.bin;
  document.getElementById("pg-msg").textContent = "";
  document.getElementById("pg-planos").innerHTML = '<div class="sk" style="height:120px"></div>';
  const r = await pgCarregarPlanos({ bin });
  if (!fatAindaAqui("#/pagar/cartao") || bin !== PG.bin) return;
  if (r.erro) { PG.planos = null; pgDesenharPlanosNovo(); document.getElementById("pg-msg").textContent = r.erro; return; }
  pgDesenharPlanosNovo();
}
function pgDesenharPlanosNovo() {
  const box = document.getElementById("pg-planos"), btn = document.getElementById("pg-btn"), rot = document.getElementById("pg-rot-parc");
  if (!box) return;
  box.innerHTML = PG.planos ? pgListaPlanos("pgDesenharPlanosNovo()") : "";
  rot.style.display = PG.planos ? "" : "none";
  const p = pgPlano();
  btn.disabled = !p;
  btn.textContent = p ? `Pagar ${p.parcelas}x de ${brl(p.valor_parcela)}` : "Digite o número do cartão";
}

// ---------------------------------------------------------------- SDK do PagBank (só quando vai pagar com cartão)
function pgSdk() {
  if (window.PagSeguro) return Promise.resolve(window.PagSeguro);
  if (pgSdk._p) return pgSdk._p;
  pgSdk._p = new Promise((ok, falha) => {
    const s = document.createElement("script");
    s.src = (PG.cfg && PG.cfg.sdk_url) || PG_SDK;
    const t = setTimeout(() => falha(new Error("tempo")), 20000);
    s.onload = () => { clearTimeout(t); window.PagSeguro ? ok(window.PagSeguro) : falha(new Error("sdk")); };
    s.onerror = () => { clearTimeout(t); falha(new Error("rede")); };
    document.head.appendChild(s);
  }).catch(e => { pgSdk._p = null; throw e; });
  return pgSdk._p;
}

const PG_ERROS_SDK = {
  INVALID_NUMBER: "Número do cartão inválido.", INVALID_SECURITY_CODE: "Código de segurança (CVV) inválido.",
  INVALID_EXPIRATION_MONTH: "Mês de validade inválido.", INVALID_EXPIRATION_YEAR: "Ano de validade inválido.",
  INVALID_HOLDER: "Confira o nome impresso no cartão.", INVALID_PUBLIC_KEY: "Pagamento com cartão indisponível agora. Tente o Pix.",
};

// 3DS (confirmação no app do banco): o SDK precisa do endereço de cobrança. O cadastro do app ainda não tem
// endereço; quando o servidor mandar `endereco` no /config, o 3DS liga sozinho. Sem ele, o crédito segue sem 3DS.
async function pgTresDS(PS, plano, card) {
  const end = PG.cfg.endereco, me = S.me || {};
  if (!end || !me.email || !me.telefone || String(me.nome || "").trim().split(/\s+/).length < 2) return null;
  try {
    const s = await api("/cashback/api/cartao/sessao3ds", {});
    if (!s.ok || !s.session) return null;
    PS.setUp({ session: s.session, env: s.env || PG.cfg.env_sdk || "SANDBOX" });
    const tel = soDigitos(me.telefone);
    const r = await PS.authenticate3DS({
      data: {
        customer: { name: me.nome, email: me.email, phones: [{ country: "55", area: tel.slice(0, 2), number: tel.slice(2), type: "MOBILE" }] },
        paymentMethod: { type: "CREDIT_CARD", installments: plano.parcelas, card },
        amount: { value: Math.round(Number(plano.total) * 100), currency: "BRL" },
        billingAddress: { street: end.rua, number: end.numero, complement: end.complemento || "", regionCode: end.uf,
                          country: "BRA", city: end.cidade, postalCode: soDigitos(end.cep) },
        dataOnly: false,
      },
    });
    if (r && r.status === "AUTH_FLOW_COMPLETED") return r.id;
    if (r && r.status === "CHANGE_PAYMENT_METHOD") throw new Error("O banco pediu outra forma de pagamento. Tente outro cartão ou o Pix.");
    return null;                                             // AUTH_NOT_SUPPORTED: crédito segue sem 3DS
  } catch (e) {
    if (String(e.message || "").indexOf("O banco") === 0) throw e;
    return null;
  }
}

// ---------------------------------------------------------------- pagar
async function pgPagar() {
  if (PG.pagando) return;
  const msg = document.getElementById("pg-msg"), btn = document.getElementById("pg-btn");
  const plano = pgPlano();
  msg.textContent = "";
  if (!plano) { msg.textContent = "Escolha em quantas vezes."; return; }
  const corpo = Object.assign(pgCorpo(), { parcelas: plano.parcelas });
  const salvo = pgSalvo();
  const cvv = soDigitos((document.getElementById("pg-cvv") || {}).value || "");
  if (cvv.length < 3) { msg.textContent = "Digite o código de segurança (CVV) do cartão."; return; }
  PG.pagando = true;
  const txtBtn = btn.textContent;
  btn.disabled = true; btn.textContent = "Pagando…";
  const falhou = t => { PG.pagando = false; btn.disabled = false; btn.textContent = txtBtn; msg.textContent = t; };
  try {
    if (salvo) {
      Object.assign(corpo, { cartao_salvo_id: salvo.id, cvv });       // cartão salvo: o servidor usa o token do PagBank
    } else {
      const num = soDigitos(document.getElementById("pg-num").value);
      const [mm, aa] = (document.getElementById("pg-val").value || "").split("/");
      const nome = String(document.getElementById("pg-nome").value || "").trim().toUpperCase();
      if (num.length < 13) return falhou("Confira o número do cartão.");
      if (!/^\d{2}$/.test(mm || "") || !/^\d{2}$/.test(aa || "") || Number(mm) < 1 || Number(mm) > 12) return falhou("Confira a validade (MM/AA).");
      if (nome.split(/\s+/).length < 2) return falhou("Digite o nome como está impresso no cartão.");
      let PS;
      try { PS = await pgSdk(); } catch (e) { return falhou("Não consegui abrir o pagamento seguro do PagBank. Confira a internet e tente de novo."); }
      const enc = PS.encryptCard({ publicKey: PG.cfg.public_key, holder: nome, number: num, expMonth: mm, expYear: "20" + aa, securityCode: cvv });
      if (!enc || enc.hasErrors) {
        const e = (enc && enc.errors && enc.errors[0]) || {};
        return falhou(PG_ERROS_SDK[e.code] || e.message || "Confira os dados do cartão.");
      }
      Object.assign(corpo, { encrypted: enc.encryptedCard, bin: num.slice(0, 6), titular: nome,
                             salvar: !!(document.getElementById("pg-salvar") || {}).checked });
      const auth = await pgTresDS(PS, plano, { encrypted: enc.encryptedCard });
      if (auth) corpo.auth3ds_id = auth;
    }
    const r = await api("/cashback/api/cartao/pagar", corpo);
    pgResultado(r, falhou);
  } catch (e) {
    falhou(e.message || "Não deu certo agora. Tente de novo.");
  }
}

function pgResultado(r, falhou) {
  if (r && r.status === "paga") { PG.res = r; PG.pagando = false; ir("#/pagar/ok"); return; }
  if (r && r.status === "recusada") return falhou(r.erro || "O banco recusou o pagamento. Tente outro cartão ou o Pix.");
  if (r && (r.status === "analise" || (r.http === 409 && r.id))) return pgAcompanhar(r.id, r.aviso || r.erro);
  if (r && r.http === 502) return falhou("Não tivemos resposta do PagBank. Antes de tentar de novo, confira no app do seu banco se a compra não foi aprovada.");
  return falhou((r && r.erro) || "Não consegui pagar agora. Tente de novo ou use o Pix.");
}

// em análise (ou pagamento já em andamento): acompanha a cada 5 s, por até 3 min
function pgAcompanhar(id, aviso) {
  const msg = document.getElementById("pg-msg"), btn = document.getElementById("pg-btn");
  if (msg) { msg.className = "msg"; msg.textContent = (aviso ? aviso + " " : "") + "Aguardando a confirmação do banco…"; }
  if (btn) { btn.disabled = true; btn.textContent = "Aguardando o banco…"; }
  let n = 0;
  if (PG.timer) clearInterval(PG.timer);
  PG.timer = setInterval(async () => {
    n++;
    const r = await api("/cashback/api/cartao/" + encodeURIComponent(id));
    if (r && r.status === "paga") { pgParar(); PG.res = r; ir("#/pagar/ok"); return; }
    if ((r && r.status === "recusada") || n >= 36) {
      pgParar();
      if (btn) { btn.disabled = false; btn.textContent = "Tentar de novo"; }
      if (msg) { msg.className = "msg erro"; msg.textContent = r && r.status === "recusada" ? (r.erro || "O banco recusou o pagamento.")
        : "O banco ainda não confirmou. Veja o resultado em Faturas daqui a pouco."; }
    }
  }, 5000);
}

// ---------------------------------------------------------------- aprovado
function pgTelaOk() {
  const r = PG.res;
  if (!r) { ir("#/faturas"); return; }
  const posto = PG.alvo.posto;
  if (typeof fatLimparCache === "function") fatLimparCache();
  el().innerHTML = `<div class="tela">${topoSimples("Pago", false)}<div class="secao" style="padding-top:2px">
    <div class="card" style="text-align:center;padding:18px 16px"><div class="pg-ok">✓</div>
      <div style="font-size:1.15rem;font-weight:800">Pagamento aprovado</div>
      <div class="s" style="color:var(--mut);margin-top:4px">A baixa já foi feita no sistema.</div></div>
    <div class="card pg-linhas">
      <div><span>${esc(PG.alvo.titulo)}</span><b>${brl(r.valor != null ? r.valor : PG.alvo.valor)}</b></div>
      <div><span>Forma</span><span>${esc(pgBandeira(r.bandeira))}${r.final ? " •••• " + esc(r.final) : ""}</span></div>
      <div><span>Parcelas</span><span>${Number(r.parcelas || 1)}x de ${brl(r.valor_parcela != null ? r.valor_parcela : r.valor)}</span></div>
      <div><span>Total no cartão</span><b>${brl(r.total != null ? r.total : r.valor)}</b></div>
      ${r.autorizacao ? `<div><span>Autorização</span><span>${esc(r.autorizacao)}</span></div>` : ""}
      <div><span>Data</span><span>${esc(new Date().toLocaleString("pt-BR", { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit" }))}</span></div>
    </div>
    <button class="btn" style="margin-top:12px" onclick="PG.alvo=null;PG.res=null;ir('#/faturas${posto ? "/" + esc(posto) : ""}')">Voltar às faturas</button>
  </div></div>`;
}
