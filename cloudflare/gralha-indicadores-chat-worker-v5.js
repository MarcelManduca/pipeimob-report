const MCP_URL =
  "https://kmysinxpdkeszrtdyhid.supabase.co/functions/v1/gralha-indicadores-mcp/mcp";

const SECURITY_HEADERS = {
  "Content-Security-Policy":
    "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'; img-src 'self' data:; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'",
  "Referrer-Policy": "no-referrer",
  "X-Content-Type-Options": "nosniff",
  "X-Frame-Options": "DENY",
  "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
};

function response(
  body,
  status = 200,
  contentType = "text/html; charset=UTF-8",
) {
  return new Response(body, {
    status,
    headers: {
      ...SECURITY_HEADERS,
      "Content-Type": contentType,
      "Cache-Control": contentType.startsWith("text/html")
        ? "no-store"
        : "no-store, max-age=0",
    },
  });
}

function json(payload, status = 200) {
  return response(
    JSON.stringify(payload),
    status,
    "application/json; charset=UTF-8",
  );
}

function configured(env) {
  return Boolean(
    env.SUPABASE_URL && env.SUPABASE_PUBLISHABLE_KEY && env.OPENAI_API_KEY,
  );
}

function authConfigured(env) {
  return Boolean(env.SUPABASE_URL && env.SUPABASE_PUBLISHABLE_KEY);
}

function supabaseUrl(env, path) {
  return `${String(env.SUPABASE_URL).replace(/\/$/, "")}${path}`;
}

async function parseJson(request) {
  const length = Number(request.headers.get("content-length") || "0");
  if (length > 55000) throw new Error("payload_too_large");
  return request.json();
}

async function login(request, env) {
  if (!authConfigured(env))
    return json(
      { error: "A configuração segura do portal ainda não foi concluída." },
      503,
    );
  let body;
  try {
    body = await parseJson(request);
  } catch {
    return json({ error: "Dados de acesso inválidos." }, 400);
  }
  const email =
    typeof body.email === "string" ? body.email.trim().toLowerCase() : "";
  const password = typeof body.password === "string" ? body.password : "";
  if (!email || !password || email.length > 254 || password.length > 512) {
    return json({ error: "Informe o e-mail e a senha." }, 400);
  }
  const auth = await fetch(
    supabaseUrl(env, "/auth/v1/token?grant_type=password"),
    {
      method: "POST",
      headers: {
        apikey: env.SUPABASE_PUBLISHABLE_KEY,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ email, password }),
    },
  );
  if (!auth.ok)
    return json(
      {
        error:
          "E-mail ou senha incorretos. Confira os dados e tente novamente.",
      },
      401,
    );
  const session = await auth.json();
  return json({
    access_token: session.access_token,
    refresh_token: session.refresh_token,
    expires_at: session.expires_at,
    user: { email: session.user?.email || email },
  });
}

async function recoverPassword(request, env) {
  if (!authConfigured(env))
    return json(
      { error: "A configuração segura do portal ainda não foi concluída." },
      503,
    );
  let body;
  try {
    body = await parseJson(request);
  } catch {
    return json({ error: "Informe um e-mail válido." }, 400);
  }
  const email =
    typeof body.email === "string" ? body.email.trim().toLowerCase() : "";
  if (!email || email.length > 254 || !email.includes("@")) {
    return json({ error: "Informe um e-mail válido." }, 400);
  }

  const redirectTo = `${new URL(request.url).origin}/reset-password`;
  const auth = await fetch(
    supabaseUrl(
      env,
      `/auth/v1/recover?redirect_to=${encodeURIComponent(redirectTo)}`,
    ),
    {
      method: "POST",
      headers: {
        apikey: env.SUPABASE_PUBLISHABLE_KEY,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ email }),
    },
  );
  if (!auth.ok)
    console.error("password_recovery_failed", { status: auth.status });

  return json({
    message:
      "Se o e-mail estiver autorizado, você receberá as instruções para criar uma nova senha.",
  });
}

async function updatePassword(request, env) {
  if (!authConfigured(env))
    return json(
      { error: "A configuração segura do portal ainda não foi concluída." },
      503,
    );
  const accessToken = bearer(request);
  if (!accessToken || accessToken.length > 8192) {
    return json(
      {
        error:
          "Este link de recuperação é inválido ou expirou. Solicite um novo.",
      },
      401,
    );
  }

  let body;
  try {
    body = await parseJson(request);
  } catch {
    return json({ error: "Informe uma nova senha válida." }, 400);
  }
  const password = typeof body.password === "string" ? body.password : "";
  if (password.length < 12 || password.length > 512) {
    return json(
      { error: "A nova senha deve ter pelo menos 12 caracteres." },
      400,
    );
  }

  const auth = await fetch(supabaseUrl(env, "/auth/v1/user"), {
    method: "PUT",
    headers: {
      apikey: env.SUPABASE_PUBLISHABLE_KEY,
      Authorization: `Bearer ${accessToken}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ password }),
  });
  if (!auth.ok) {
    return json(
      {
        error:
          "Não foi possível atualizar a senha. O link pode ter expirado; solicite um novo.",
      },
      401,
    );
  }
  return json({
    message:
      "Senha criada com sucesso. Agora você já pode entrar no Gralha Indicadores.",
  });
}

async function refresh(request, env) {
  if (!configured(env)) return json({ error: "Portal não configurado." }, 503);
  let body;
  try {
    body = await parseJson(request);
  } catch {
    return json({ error: "Sessão inválida." }, 400);
  }
  const refreshToken =
    typeof body.refresh_token === "string" ? body.refresh_token : "";
  if (!refreshToken || refreshToken.length > 4096)
    return json({ error: "Sessão inválida." }, 401);
  const auth = await fetch(
    supabaseUrl(env, "/auth/v1/token?grant_type=refresh_token"),
    {
      method: "POST",
      headers: {
        apikey: env.SUPABASE_PUBLISHABLE_KEY,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ refresh_token: refreshToken }),
    },
  );
  if (!auth.ok)
    return json({ error: "Sua sessão expirou. Entre novamente." }, 401);
  const session = await auth.json();
  return json({
    access_token: session.access_token,
    refresh_token: session.refresh_token,
    expires_at: session.expires_at,
    user: { email: session.user?.email || "" },
  });
}

function bearer(request) {
  const header = request.headers.get("Authorization") || "";
  return header.startsWith("Bearer ") ? header.slice(7).trim() : "";
}

function outputText(payload) {
  if (typeof payload.output_text === "string" && payload.output_text.trim()) {
    return payload.output_text.trim();
  }
  const parts = [];
  for (const item of Array.isArray(payload.output) ? payload.output : []) {
    for (const block of Array.isArray(item?.content) ? item.content : []) {
      if (typeof block?.text === "string") parts.push(block.text);
    }
  }
  return parts.join("\n").trim();
}

function parseJsonString(value) {
  if (typeof value !== "string") return value;
  try {
    return JSON.parse(value);
  } catch {
    return null;
  }
}

function structuredToolOutput(payload) {
  const calls = (Array.isArray(payload.output) ? payload.output : [])
    .filter((item) => item?.type === "mcp_call" && !item.error)
    .reverse();
  for (const call of calls) {
    const parsed = parseJsonString(call.output);
    const candidates = [
      parsed?.structuredContent,
      parsed?.result?.structuredContent,
      parsed?.result,
      parsed,
    ];
    for (const candidate of candidates) {
      if (
        candidate &&
        typeof candidate === "object" &&
        candidate.visualization
      ) {
        return candidate;
      }
    }
    const content = parsed?.content ?? parsed?.result?.content;
    for (const block of Array.isArray(content) ? content : []) {
      const value = parseJsonString(block?.text);
      if (value && typeof value === "object" && value.visualization)
        return value;
    }
  }
  return null;
}

function safeVisualization(value) {
  if (!value || value.type !== "bar" || !Array.isArray(value.series))
    return null;
  const series = value.series.slice(0, 10).flatMap((item) => {
    const label =
      typeof item?.label === "string" ? item.label.trim().slice(0, 80) : "";
    const numericValue = Number(item?.value);
    if (!label || !Number.isFinite(numericValue) || numericValue < 0) return [];
    return [
      {
        label,
        value: numericValue,
        sales_count: Number.isFinite(Number(item.sales_count))
          ? Number(item.sales_count)
          : null,
        vgv: Number.isFinite(Number(item.vgv)) ? Number(item.vgv) : null,
      },
    ];
  });
  if (!series.length) return null;
  return {
    type: "bar",
    title:
      typeof value.title === "string"
        ? value.title.slice(0, 140)
        : "Comparativo",
    metric: value.metric === "vgv" ? "vgv" : "sales_count",
    unit: value.unit === "BRL" ? "BRL" : "sales",
    series,
    footnote:
      typeof value.footnote === "string" ? value.footnote.slice(0, 320) : "",
  };
}

function todayInSaoPaulo() {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/Sao_Paulo",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(new Date());
  const values = Object.fromEntries(
    parts.map((part) => [part.type, part.value]),
  );
  return `${values.year}-${values.month}-${values.day}`;
}

async function chat(request, env) {
  if (!configured(env))
    return json(
      { error: "A configuração segura do portal ainda não foi concluída." },
      503,
    );
  const accessToken = bearer(request);
  if (!accessToken || accessToken.length > 8192)
    return json({ error: "Sessão ausente. Entre novamente." }, 401);

  const authCheck = await fetch(supabaseUrl(env, "/auth/v1/user"), {
    headers: {
      apikey: env.SUPABASE_PUBLISHABLE_KEY,
      Authorization: `Bearer ${accessToken}`,
    },
  });
  if (!authCheck.ok)
    return json(
      { error: "Sua sessão expirou. Entre novamente para continuar." },
      401,
    );

  let body;
  try {
    body = await parseJson(request);
  } catch {
    return json({ error: "Pergunta inválida." }, 400);
  }

  const messages = Array.isArray(body.messages) ? body.messages.slice(-12) : [];
  const valid =
    messages.length > 0 &&
    messages.every(
      (message) =>
        (message?.role === "user" || message?.role === "assistant") &&
        typeof message?.content === "string" &&
        message.content.trim().length > 0 &&
        message.content.length <= 4000,
    );
  if (!valid) return json({ error: "Digite uma pergunta válida." }, 400);
  const latestQuestion = messages.at(-1)?.content || "";
  const currentDate = todayInSaoPaulo();

  const ai = await fetch("https://api.openai.com/v1/responses", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.OPENAI_API_KEY}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      model: env.OPENAI_MODEL || "gpt-5-mini",
      store: false,
      max_output_tokens: 1400,
      instructions: [
        "Você é o assistente de indicadores comerciais da Gralha Imóveis.",
        "Responda sempre em português do Brasil, de forma executiva, clara e objetiva.",
        "Para perguntas sobre vendas oficiais, rankings, corretores, equipes, bairros, VGV ou ticket, use consultar_ranking_vendas. Para perguntas sobre negócios cadastrados no período, status geral ou etapa atual do funil, use consultar_funil_vista.",
        "Nunca invente valores, nomes, posições, critérios ou períodos.",
        `A data atual em São Paulo é ${currentDate}. Nunca trate datas posteriores como já realizadas; para ano corrente, consulte do primeiro dia do ano até a data atual.`,
        "Informe o período efetivo, o critério, a atualização das fontes e a cobertura de atribuição. Diferencie dados vivos das APIs da referência gerencial de equipes.",
        "Em avaliações de desempenho, separe fatos, métricas calculadas, interpretação comparativa e limitações. No funil, diferencie negócios criados no período, etapa atual, status geral e eventos históricos de entrada em etapa. Nunca trate negócios atualmente em Proposta como propostas geradas no período. Não atribua causas nem afirme conversão, visitas, propostas geradas ou tempo entre etapas sem dados operacionais correspondentes.",
        "Quando a métrica histórica solicitada estiver indisponível, não comece pela limitação. Responda em no máximo 90 palavras e abra com 'Para gestão atual', apresentando primeiro o total com status geral Em aberto na etapa solicitada e depois o total atual nessa etapa. Diga que esses são os números operacionais confiáveis da fotografia atual. Em seguida, esclareça em uma única frase que eles não representam todas as entradas históricas na etapa porque esse histórico não está disponível. Não repita a pergunta, não liste fontes, timestamps ou detalhes técnicos salvo se solicitados, não diga que falta confirmação de contrato e não ofereça alternativas irrelevantes.",
        "Quando o usuário pedir Top N, envie top_n=N à ferramenta. O padrão é Top 10.",
        "Nunca produza gráficos com caracteres, código Python, matplotlib ou instruções para gerar imagem. O portal renderiza a visualização estruturada devolvida pela ferramenta.",
        "Quando útil, use listas curtas em texto simples e evite repetir a mesma confirmação.",
        "Se a ferramenta não trouxer dados suficientes, explique objetivamente o que falta.",
      ].join(" "),
      input: messages.map(({ role, content }) => ({ role, content })),
      tools: [
        {
          type: "mcp",
          server_label: "gralha_indicadores",
          server_description:
            "Consulta somente leitura aos rankings de vendas e à coorte de negócios do funil Vista autorizados da Gralha Imóveis.",
          server_url: MCP_URL,
          authorization: accessToken,
          allowed_tools: ["consultar_ranking_vendas", "consultar_funil_vista"],
          require_approval: "never",
        },
      ],
    }),
  });

  const payload = await ai.json().catch(() => ({}));
  if (!ai.ok) {
    console.error("openai_request_failed", {
      status: ai.status,
      requestId: ai.headers.get("x-request-id"),
    });
    if (ai.status === 429)
      return json(
        {
          error:
            "O limite temporário de consultas foi atingido. Aguarde um pouco e tente novamente.",
        },
        429,
      );
    return json(
      {
        error:
          "Não foi possível consultar os indicadores agora. Tente novamente em instantes.",
      },
      502,
    );
  }
  const answer = outputText(payload);
  if (!answer)
    return json(
      { error: "A consulta terminou sem uma resposta legível." },
      502,
    );
  const structured = structuredToolOutput(payload);
  const visualizationRequested =
    /gr[aá]fic|visual|compar|ranking|top\s*\d/i.test(latestQuestion);
  const visualization = visualizationRequested
    ? safeVisualization(structured?.visualization)
    : null;
  return json({ answer, visualization });
}

const HTML = `<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
  <meta name="description" content="Assistente executivo de indicadores comerciais da Gralha Imóveis">
  <meta name="theme-color" content="#0d3626">
  <title>Gralha Indicadores Chat</title>
  <style>
    :root{--ink:#17221d;--muted:#68756e;--paper:#f5f4ee;--surface:#fffef9;--forest:#164d36;--deep:#0d3626;--lime:#c9e76f;--line:#dfe3da;--danger:#b42318}
    *{box-sizing:border-box}html,body{min-height:100%;margin:0}body{background:var(--paper);color:var(--ink);font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}button,input,textarea{font:inherit}button{cursor:pointer}.hidden{display:none!important}
    .brand{display:flex;align-items:center;gap:12px;font-weight:780;letter-spacing:-.02em}.brand small{display:block;color:var(--muted);font-size:11px;font-weight:550;letter-spacing:0}.mark{width:42px;height:42px;display:grid;place-items:center;border-radius:14px 14px 6px 14px;color:var(--deep);background:var(--lime);font-weight:900;font-size:20px;box-shadow:inset 0 0 0 1px rgba(13,54,38,.09)}
    .eyebrow{margin:0 0 14px;color:var(--forest);font-size:12px;font-weight:850;letter-spacing:.14em;text-transform:uppercase}.login{min-height:100vh;display:grid;grid-template-columns:minmax(0,1.05fr) minmax(420px,.95fr)}.story{position:relative;display:flex;flex-direction:column;justify-content:space-between;padding:48px clamp(38px,7vw,110px);color:#f8fff9;overflow:hidden;background:var(--deep)}.story:before{content:"";position:absolute;right:-12%;bottom:-24%;width:min(700px,72vw);aspect-ratio:1;border-radius:50%;border:1px solid rgba(201,231,111,.35);box-shadow:0 0 0 80px rgba(201,231,111,.035),0 0 0 170px rgba(201,231,111,.025)}.story>*{position:relative;z-index:1}.story .eyebrow{color:var(--lime)}.story h1{margin:0;font-size:clamp(54px,7vw,104px);line-height:.88;letter-spacing:-.072em}.story-copy{max-width:570px;margin:32px 0 0;font-size:clamp(17px,1.5vw,21px);line-height:1.6;color:rgba(248,255,249,.72)}.security{color:rgba(248,255,249,.7);font-size:13px}.panel{display:grid;place-items:center;min-height:100vh;padding:38px;background:var(--surface)}form{width:min(420px,100%);display:grid;gap:22px}form h2{margin:0 0 8px;font-size:clamp(32px,4vw,46px);letter-spacing:-.045em}form .intro{margin:0;color:var(--muted);line-height:1.55}label{display:grid;gap:8px;color:#445149;font-size:13px;font-weight:750}input{height:48px;padding:0 14px;border:1px solid #d8ddd5;border-radius:10px;background:white;color:var(--ink);outline:none}input:focus,textarea:focus{border-color:var(--forest);box-shadow:0 0 0 3px rgba(22,77,54,.12)}.primary{height:50px;border:0;border-radius:12px;background:var(--forest);color:white;font-weight:780}.primary:hover{background:var(--deep)}.primary:disabled{cursor:not-allowed;opacity:.58}.link-button{justify-self:start;padding:0;border:0;background:transparent;color:var(--forest);font-weight:750;text-decoration:underline;text-underline-offset:3px}.error,.success{margin:0;font-size:13px;line-height:1.5}.error{color:var(--danger)}.success{color:var(--forest)}
    .chat{min-height:100vh;display:flex;flex-direction:column}.header{height:74px;display:flex;align-items:center;justify-content:space-between;padding:0 clamp(18px,4vw,54px);background:rgba(255,254,249,.92);border-bottom:1px solid var(--line);backdrop-filter:blur(14px)}.header .mark{width:38px;height:38px;border-radius:12px 12px 5px 12px}.account{display:flex;align-items:center;gap:18px}.connected{display:flex;align-items:center;gap:9px;color:var(--muted);font-size:12px;font-weight:650}.dot{width:7px;height:7px;border-radius:50%;background:#2d9d64;box-shadow:0 0 0 4px rgba(45,157,100,.12)}.logout{border:0;background:transparent;color:var(--muted);font-weight:700;padding:9px}.workspace{min-height:0;flex:1;display:flex;flex-direction:column}.messages{min-height:0;flex:1;overflow:auto;padding:40px clamp(18px,4vw,54px) 28px}.welcome{width:min(820px,100%);margin:clamp(34px,8vh,100px) auto 0;text-align:center}.orb{width:58px;height:58px;display:grid;place-items:center;margin:0 auto 24px;color:var(--deep);background:var(--lime);border-radius:20px 20px 7px 20px;font-size:25px;box-shadow:0 14px 40px rgba(75,103,37,.2)}.welcome h1{margin:0;font-size:clamp(35px,5vw,62px);line-height:1.02;letter-spacing:-.055em}.welcome>.copy{max-width:610px;margin:20px auto 0;color:var(--muted);line-height:1.65}.suggestions{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-top:36px;text-align:left}.suggestions button{min-height:108px;padding:18px;color:#344139;background:rgba(255,254,249,.78);border:1px solid var(--line);border-radius:16px;text-align:left;font-weight:620;transition:.18s}.suggestions button:hover{transform:translateY(-2px);border-color:#aab9aa;background:white}.conversation{width:min(900px,100%);margin:0 auto;display:grid;gap:28px}.message{display:grid;grid-template-columns:78px minmax(0,1fr);gap:16px;align-items:start}.who{padding-top:3px;color:var(--muted);font-size:12px;font-weight:800;text-transform:uppercase;letter-spacing:.08em}.bubble{white-space:pre-wrap;margin:0;font-size:16px;line-height:1.75}.user .bubble{display:inline-block;padding:15px 18px;background:white;border:1px solid var(--line);border-radius:4px 18px 18px 18px}.typing{display:flex;gap:5px;padding-top:8px}.typing i{width:7px;height:7px;border-radius:50%;background:var(--forest);animation:bounce 1s infinite ease-in-out}.typing i:nth-child(2){animation-delay:.14s}.typing i:nth-child(3){animation-delay:.28s}@keyframes bounce{0%,60%,100%{transform:translateY(0);opacity:.35}30%{transform:translateY(-5px);opacity:1}}
    .assistant-content{min-width:0}.assistant-content>.bubble{white-space:pre-wrap}.chart{margin:22px 0 0;padding:20px;background:rgba(255,254,249,.78);border:1px solid var(--line);border-radius:16px}.chart figcaption{margin:0 0 14px;font-size:15px;font-weight:800;color:var(--ink)}.chart svg{display:block;width:100%;height:auto;overflow:visible}.chart .label{fill:var(--ink);font-size:13px;font-weight:650}.chart .value{fill:var(--forest);font-size:12px;font-weight:800}.chart .bar-bg{fill:#e8ece4}.chart .bar{fill:var(--forest)}.chart .footnote{margin:12px 0 0;color:var(--muted);font-size:11px;line-height:1.5}
    .composer-wrap{width:min(900px,calc(100% - 36px));margin:0 auto;padding:0 0 18px}.composer{display:flex;align-items:flex-end;gap:10px;padding:10px 10px 10px 16px;background:white;border:1px solid #d6ddd4;border-radius:18px;box-shadow:0 16px 50px rgba(23,34,29,.1)}textarea{width:100%;min-height:40px;max-height:140px;padding:9px 0;resize:none;border:0;background:transparent;color:var(--ink);outline:none;line-height:1.5}textarea:focus{box-shadow:none}.send{width:42px;height:42px;flex:0 0 auto;border:0;border-radius:12px;background:var(--forest);color:white;font-size:19px;font-weight:800}.send:disabled{opacity:.45}.notice{margin:9px 0 0;color:#849087;text-align:center;font-size:11px}.chat-error{margin:0 0 8px 4px}
    @media(max-width:840px){.login{grid-template-columns:1fr}.story{min-height:42vh;padding:28px 24px 34px}.story h1{margin-top:70px;font-size:clamp(48px,14vw,74px)}.story-copy{margin-top:22px}.security{display:none}.panel{min-height:58vh;padding:44px 24px}.suggestions{grid-template-columns:1fr}.suggestions button{min-height:76px}}
    @media(max-width:600px){.header{height:66px}.connected{display:none}.messages{padding-top:28px}.welcome{margin-top:28px}.message{grid-template-columns:1fr;gap:7px}.who{padding:0}.logout{font-size:12px}}
    @media(prefers-reduced-motion:reduce){*,*:before,*:after{animation-duration:.01ms!important;animation-iteration-count:1!important;transition-duration:.01ms!important}}
  </style>
</head>
<body>
  <main id="login" class="login">
    <section class="story" aria-label="Gralha Indicadores">
      <div class="brand"><div class="mark">▥</div><span>Gralha Indicadores</span></div>
      <div><p class="eyebrow">Inteligência comercial</p><h1>Seus números,<br>em conversa.</h1><p class="story-copy">Consulte rankings, desempenho e evolução de vendas com linguagem simples e dados protegidos.</p></div>
      <div class="security">✓ Acesso restrito e dados em tempo real</div>
    </section>
    <section class="panel">
      <form id="login-form">
        <div><p class="eyebrow">Área exclusiva</p><h2>Entre para consultar</h2><p class="intro">Use o mesmo acesso autorizado no Gralha Indicadores.</p></div>
        <label>E-mail corporativo<input id="email" type="email" autocomplete="email" required></label>
        <label>Senha<input id="password" type="password" autocomplete="current-password" required></label>
        <button id="forgot-password" class="link-button" type="button">Esqueci minha senha</button>
        <p id="login-error" class="error hidden" role="alert"></p>
        <button id="login-button" class="primary" type="submit">Entrar</button>
      </form>
      <form id="recover-form" class="hidden">
        <div><p class="eyebrow">Recuperar acesso</p><h2>Crie uma nova senha</h2><p class="intro">Informe seu e-mail autorizado. Enviaremos um link seguro para continuar.</p></div>
        <label>E-mail corporativo<input id="recover-email" type="email" autocomplete="email" required></label>
        <p id="recover-message" class="success hidden" role="status"></p>
        <p id="recover-error" class="error hidden" role="alert"></p>
        <button id="recover-button" class="primary" type="submit">Enviar link de recuperação</button>
        <button id="back-to-login" class="link-button" type="button">Voltar para entrar</button>
      </form>
      <form id="reset-form" class="hidden">
        <div><p class="eyebrow">Nova senha</p><h2>Defina sua senha</h2><p class="intro">Use pelo menos 12 caracteres e guarde-a em um gerenciador de senhas.</p></div>
        <label>Nova senha<input id="new-password" type="password" autocomplete="new-password" minlength="12" required></label>
        <label>Confirmar nova senha<input id="confirm-password" type="password" autocomplete="new-password" minlength="12" required></label>
        <p id="reset-message" class="success hidden" role="status"></p>
        <p id="reset-error" class="error hidden" role="alert"></p>
        <button id="reset-button" class="primary" type="submit">Salvar nova senha</button>
        <button id="reset-back-to-login" class="link-button hidden" type="button">Voltar para entrar</button>
      </form>
    </section>
  </main>
  <main id="chat" class="chat hidden">
    <header class="header"><div class="brand"><div class="mark">▥</div><div><strong>Gralha Indicadores</strong><small>Assistente comercial</small></div></div><div class="account"><span class="connected"><i class="dot"></i>Dados conectados</span><button id="logout" class="logout" type="button">Sair</button></div></header>
    <section class="workspace">
      <div id="messages" class="messages" aria-live="polite">
        <div id="welcome" class="welcome"><div class="orb">✦</div><p id="hello" class="eyebrow">Olá</p><h1>O que você quer saber sobre as vendas?</h1><p class="copy">Faça uma pergunta em linguagem natural. As respostas usam os dados autorizados do Gralha Indicadores.</p><div id="suggestions" class="suggestions"><button type="button">Quem lidera o ranking de vendas no mês?</button><button type="button">Compare o desempenho das equipes no período atual.</button><button type="button">Quais corretores mais cresceram recentemente?</button></div></div>
        <div id="conversation" class="conversation hidden"></div>
      </div>
      <div class="composer-wrap"><p id="chat-error" class="error chat-error hidden" role="alert"></p><div class="composer"><textarea id="draft" aria-label="Digite sua pergunta" placeholder="Pergunte sobre rankings, equipes, corretores ou períodos…" rows="1" maxlength="4000"></textarea><button id="send" class="send" type="button" aria-label="Enviar pergunta">↑</button></div><p class="notice">As respostas podem conter imprecisões. Confirme decisões críticas no dashboard oficial.</p></div>
    </section>
  </main>
  <script>
  (()=>{
    const KEY="gralha-indicadores-session";let session=null,messages=[],busy=false;
    const $=id=>document.getElementById(id), login=$("login"), chat=$("chat"), form=$("login-form"), recoverForm=$("recover-form"), resetForm=$("reset-form"), loginError=$("login-error"), loginButton=$("login-button"), conversation=$("conversation"), welcome=$("welcome"), draft=$("draft"), send=$("send"), chatError=$("chat-error");
    const showError=(el,text)=>{el.textContent=text;el.classList.toggle("hidden",!text)};
    const showAuthForm=name=>{form.classList.toggle("hidden",name!=="login");recoverForm.classList.toggle("hidden",name!=="recover");resetForm.classList.toggle("hidden",name!=="reset")};
    const save=value=>{session=value;if(value)localStorage.setItem(KEY,JSON.stringify(value));else localStorage.removeItem(KEY)};
    const read=()=>{try{return JSON.parse(localStorage.getItem(KEY)||"null")}catch{return null}};
    const showChat=()=>{login.classList.add("hidden");chat.classList.remove("hidden");const name=(session?.user?.email||"diretor").split("@")[0].replace(/[._-]+/g," ");$("hello").textContent="Olá, "+name};
    const showLogin=()=>{chat.classList.add("hidden");login.classList.remove("hidden")};
    const api=async(path,body,token)=>fetch(path,{method:"POST",headers:{"Content-Type":"application/json",...(token?{Authorization:"Bearer "+token}:{})},body:JSON.stringify(body)});
    const renew=async()=>{if(!session?.refresh_token)return false;const res=await api("/api/refresh",{refresh_token:session.refresh_token});if(!res.ok){save(null);return false}save(await res.json());return true};
    form.addEventListener("submit",async e=>{e.preventDefault();showError(loginError,"");loginButton.disabled=true;loginButton.textContent="Entrando…";try{const res=await api("/api/login",{email:$("email").value.trim(),password:$("password").value});const data=await res.json();if(!res.ok){showError(loginError,data.error||"Não foi possível entrar.");return}save(data);$("password").value="";showChat()}catch{showError(loginError,"Não foi possível conectar agora. Tente novamente em instantes.")}finally{loginButton.disabled=false;loginButton.textContent="Entrar"}});
    $("forgot-password").addEventListener("click",()=>{$("recover-email").value=$("email").value.trim();showAuthForm("recover")});
    $("back-to-login").addEventListener("click",()=>showAuthForm("login"));
    recoverForm.addEventListener("submit",async e=>{e.preventDefault();const error=$("recover-error"),message=$("recover-message"),button=$("recover-button");showError(error,"");showError(message,"");button.disabled=true;button.textContent="Enviando…";try{const res=await api("/api/auth/recover",{email:$("recover-email").value.trim()});const data=await res.json();if(!res.ok){showError(error,data.error||"Não foi possível enviar o link.");return}showError(message,data.message||"Confira seu e-mail para continuar.")}catch{showError(error,"Não foi possível enviar o link agora. Tente novamente em instantes.")}finally{button.disabled=false;button.textContent="Enviar link de recuperação"}});
    const fragment=new URLSearchParams(location.hash.slice(1));
    const recoveryToken=fragment.get("access_token")||"";
    resetForm.addEventListener("submit",async e=>{e.preventDefault();const error=$("reset-error"),message=$("reset-message"),button=$("reset-button"),password=$("new-password").value;showError(error,"");showError(message,"");if(password!==$("confirm-password").value){showError(error,"As senhas não coincidem.");return}button.disabled=true;button.textContent="Salvando…";try{const res=await api("/api/auth/update-password",{password},recoveryToken);const data=await res.json();if(!res.ok){showError(error,data.error||"Não foi possível atualizar a senha.");return}history.replaceState(null,"","/");showError(message,data.message);button.classList.add("hidden");$("new-password").closest("label").classList.add("hidden");$("confirm-password").closest("label").classList.add("hidden");$("reset-back-to-login").classList.remove("hidden")}catch{showError(error,"Não foi possível atualizar a senha agora. Tente novamente.")}finally{button.disabled=false;button.textContent="Salvar nova senha"}});
    $("reset-back-to-login").addEventListener("click",()=>{location.href="/"});
    function chartValue(value,unit){return unit==="BRL"?new Intl.NumberFormat("pt-BR",{style:"currency",currency:"BRL",maximumFractionDigits:0}).format(value):new Intl.NumberFormat("pt-BR").format(value)+" vendas"}
    function renderChart(data){if(!data||data.type!=="bar"||!Array.isArray(data.series)||!data.series.length)return null;const rows=data.series.slice(0,10),width=760,height=54+rows.length*46,chartX=215,maxBar=400,max=Math.max(...rows.map(item=>Number(item.value)||0),1),ns="http://www.w3.org/2000/svg",figure=document.createElement("figure"),caption=document.createElement("figcaption"),svg=document.createElementNS(ns,"svg");figure.className="chart";caption.textContent=data.title||"Comparativo";svg.setAttribute("viewBox","0 0 "+width+" "+height);svg.setAttribute("role","img");svg.setAttribute("aria-label",data.title||"Gráfico de barras");rows.forEach((item,index)=>{const y=32+index*46,label=document.createElementNS(ns,"text"),background=document.createElementNS(ns,"rect"),bar=document.createElementNS(ns,"rect"),value=document.createElementNS(ns,"text"),barWidth=Math.max(2,(Number(item.value)||0)/max*maxBar);label.setAttribute("x","0");label.setAttribute("y",String(y+5));label.setAttribute("class","label");label.textContent=item.label.length>27?item.label.slice(0,26)+"…":item.label;background.setAttribute("x",String(chartX));background.setAttribute("y",String(y-14));background.setAttribute("width",String(maxBar));background.setAttribute("height","24");background.setAttribute("rx","7");background.setAttribute("class","bar-bg");bar.setAttribute("x",String(chartX));bar.setAttribute("y",String(y-14));bar.setAttribute("width",String(barWidth));bar.setAttribute("height","24");bar.setAttribute("rx","7");bar.setAttribute("class","bar");value.setAttribute("x",String(Math.min(chartX+barWidth+9,690)));value.setAttribute("y",String(y+4));value.setAttribute("class","value");value.textContent=chartValue(Number(item.value)||0,data.unit);svg.append(label,background,bar,value)});figure.append(caption,svg);if(data.footnote){const note=document.createElement("p");note.className="footnote";note.textContent=data.footnote;figure.append(note)}return figure}
    function addMessage(role,content,visualization){const article=document.createElement("article");article.className="message "+role;const who=document.createElement("span");who.className="who";who.textContent=role==="assistant"?"Gralha":"Você";const wrapper=document.createElement("div"),bubble=document.createElement("p");wrapper.className=role==="assistant"?"assistant-content":"";bubble.className="bubble";bubble.textContent=content;wrapper.append(bubble);const chart=role==="assistant"?renderChart(visualization):null;if(chart)wrapper.append(chart);article.append(who,wrapper);conversation.append(article);$("messages").scrollTo({top:$("messages").scrollHeight,behavior:"smooth"})}
    function typing(on){const old=$("typing");if(old)old.remove();if(!on)return;const article=document.createElement("article");article.id="typing";article.className="message assistant";article.innerHTML='<span class="who">Gralha</span><div class="typing"><i></i><i></i><i></i></div>';conversation.append(article);$("messages").scrollTop=$("messages").scrollHeight}
    async function ask(value){const question=(value||draft.value).trim();if(!question||busy||!session)return;busy=true;send.disabled=true;showError(chatError,"");welcome.classList.add("hidden");conversation.classList.remove("hidden");messages.push({role:"user",content:question});addMessage("user",question);draft.value="";typing(true);try{let res=await api("/api/chat",{messages:messages.slice(-12)},session.access_token);if(res.status===401&&await renew())res=await api("/api/chat",{messages:messages.slice(-12)},session.access_token);const data=await res.json();if(!res.ok||!data.answer){if(res.status===401){save(null);messages=[];conversation.textContent="";showLogin()}showError(chatError,data.error||"Não foi possível obter a resposta agora.");return}messages.push({role:"assistant",content:data.answer});addMessage("assistant",data.answer,data.visualization)}catch{showError(chatError,"A conexão foi interrompida. Tente enviar a pergunta novamente.")}finally{typing(false);busy=false;send.disabled=false}}
    send.addEventListener("click",()=>ask());draft.addEventListener("keydown",e=>{if(e.key==="Enter"&&!e.shiftKey){e.preventDefault();ask()}});$("suggestions").addEventListener("click",e=>{if(e.target.tagName==="BUTTON")ask(e.target.textContent)});$("logout").addEventListener("click",()=>{save(null);messages=[];conversation.textContent="";welcome.classList.remove("hidden");conversation.classList.add("hidden");showLogin()});
    if(location.pathname==="/reset-password"){save(null);showLogin();showAuthForm("reset");if(!recoveryToken){showError($("reset-error"),fragment.get("error_description")||"Este link de recuperação é inválido ou expirou. Solicite um novo.");$("reset-button").disabled=true}}else{session=read();if(session?.access_token&&session?.refresh_token)showChat();else{save(null);showLogin();showAuthForm("login")}}
  })();
  </script>
</body>
</html>`;

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    try {
      if (
        request.method === "GET" &&
        (url.pathname === "/" || url.pathname === "/reset-password")
      )
        return response(HTML);
      if (request.method === "GET" && url.pathname === "/api/health") {
        return json({ status: "ok", configured: configured(env) });
      }
      if (request.method === "POST" && url.pathname === "/api/login")
        return login(request, env);
      if (request.method === "POST" && url.pathname === "/api/auth/recover")
        return recoverPassword(request, env);
      if (
        request.method === "POST" &&
        url.pathname === "/api/auth/update-password"
      )
        return updatePassword(request, env);
      if (request.method === "POST" && url.pathname === "/api/refresh")
        return refresh(request, env);
      if (request.method === "POST" && url.pathname === "/api/chat")
        return chat(request, env);
      return json({ error: "Rota não encontrada." }, 404);
    } catch (error) {
      console.error("worker_error", {
        path: url.pathname,
        message: error instanceof Error ? error.message : "unknown",
      });
      return json(
        { error: "O serviço encontrou uma falha temporária. Tente novamente." },
        500,
      );
    }
  },
};
