'use strict';
(() => {
  const $ = (s) => document.querySelector(s);
  const gate = $('#authGate'), form = $('#authForm'), notice = $('#authNotice');
  let mode = 'login';
  const modes = {
    login: ['Sign in to DocMind','Your private workspace for document intelligence.','Sign in','current-password',false,false,false],
    register: ['Create your account','Start a private workspace for your documents.','Create account','new-password',true,true],
    forgot: ['Reset your password','We will send a one-time reset link if the account exists.','Send reset link','',false,false,false],
    resend: ['Resend verification','We will send a fresh verification link if the account needs it.','Resend link','',false,false,false],
    reset: ['Choose a new password','Use at least 10 characters, including a letter and a number.','Update password','new-password',false,true,false],
    verify: ['Verify your email','Complete email verification to access your workspace.','Verify email','',false,false,false]
  };
  function showMessage(text, error=false) { notice.hidden = !text; notice.textContent = text || ''; notice.className = 'auth-notice' + (error ? ' error' : ''); }
  function render(next) {
    mode=next; const [title,subtitle,submit,autocomplete,name,confirm]=modes[mode];
    $('#authTitle').textContent=title; $('#authSubtitle').textContent=subtitle; $('#authSubmit').textContent=submit;
    $('#nameField').hidden=!name; $('#authForm [name=full_name]').required=name;
    $('#confirmField').hidden=!confirm;
    $('#authForm [name=confirm_password]').minLength=confirm ? 10 : 0;
    $('#authForm [name=email]').closest('label').hidden=mode==='verify'||mode==='reset';
    $('#authForm [name=email]').required=!['verify','reset'].includes(mode);
    $('#passwordField').hidden=mode==='forgot'||mode==='verify'||mode==='resend';
    $('#passwordField input').required=!['forgot','verify','resend'].includes(mode);
    $('#passwordField input').minLength=['login'].includes(mode) ? 0 : 10;
    $('#authForm [name=confirm_password]').required=confirm;
    $('#passwordField input').autocomplete=autocomplete || 'new-password';
    $('#authLinks').hidden=mode==='verify'||mode==='reset';
    $('#authLinks').innerHTML=mode==='login' ? '<button type="button" data-auth-mode="forgot">Forgot password?</button><button type="button" data-auth-mode="resend">Resend verification</button><a href="/?auth=register">Don\'t have an account? Create account</a>' : mode==='register' ? '<a href="/?auth=login">Already have an account? Sign in</a>' : '<a href="/?auth=login">Back to sign in</a>';
    form.reset(); showMessage('');
  }
  async function request(path,body={}) { const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},credentials:'same-origin',body:JSON.stringify(body)}); const d=await r.json().catch(()=>({})); if(!r.ok) throw new Error(d.detail||'Request failed'); return d; }
  function loadApp() {
    $('#app').hidden=false; gate.hidden=true;
    ['/markdown.js','/js/core.js','/js/chat.js','/js/library.js','/js/settings.js','/js/main.js'].forEach((src)=>{const s=document.createElement('script');s.src=src;s.async=false;document.body.appendChild(s);});
  }
  async function bootstrap() {
    const params=new URLSearchParams(location.search);
    if(params.has('verify_email')) { render('verify'); form.dataset.token=params.get('verify_email'); }
    else if(params.has('reset_password')) { render('reset'); form.dataset.token=params.get('reset_password'); }
    else if(['login','register'].includes(params.get('auth'))) render(params.get('auth'));
    else {
      try { const r=await fetch('/auth/me',{credentials:'same-origin'}); if(r.ok){const d=await r.json();window.docmindUser=d.user;loadApp();return;} }
      catch {}
      render('login');
    }
  }
  $('#authLinks').addEventListener('click',(e)=>{const b=e.target.closest('[data-auth-mode]');if(b)render(b.dataset.authMode);});
  form.addEventListener('click',(e)=>{const b=e.target.closest('[data-password-toggle]');if(!b)return;const input=form.elements[b.dataset.passwordToggle];const visible=input.type==='password';input.type=visible?'text':'password';b.textContent=visible?'Hide password':'Show password';b.setAttribute('aria-pressed',String(visible));});
  form.addEventListener('input',()=>{
    if(!notice.classList.contains('error')) return;
    const password=form.elements.password.value, confirm=form.elements.confirm_password.value;
    if((notice.textContent==='Passwords do not match'&&password===confirm) ||
       (notice.textContent==='Please confirm your password.'&&confirm)) showMessage('');
  });
  form.addEventListener('submit',async(e)=>{
    e.preventDefault(); const fd=new FormData(form), email=String(fd.get('email')||'').trim(), password=String(fd.get('password')||''), confirm=String(fd.get('confirm_password')||'');
    $('#authSubmit').disabled=true; showMessage('Please wait…');
    try {
      if(mode==='login') { const d=await request('/auth/login',{email,password}); window.docmindUser=d.user; loadApp(); return; }
      if(mode==='register') {
        if(!confirm) { showMessage('Please confirm your password.',true); return; }
        if(password!==confirm) { showMessage('Passwords do not match',true); return; }
        await request('/auth/register',{full_name:fd.get('full_name'),email,password}); render('login'); showMessage('Account created. Check the server email logs for your local verification link, or your inbox if email is configured.'); return;
      }
      if(mode==='forgot') { await request('/auth/forgot-password',{email}); showMessage('If the account exists, a password reset link has been sent.'); return; }
      if(mode==='resend') { await request('/auth/resend-verification',{email}); showMessage('If the account needs verification, a fresh link has been sent.'); return; }
      if(mode==='verify') { await request('/auth/verify-email',{token:form.dataset.token}); history.replaceState({},'', '/'); render('login'); showMessage('Email verified. You can now sign in.'); return; }
      if(mode==='reset') { if(password!==confirm) throw new Error('Passwords do not match'); await request('/auth/reset-password',{token:form.dataset.token,password,confirm_password:confirm}); history.replaceState({},'', '/'); render('login'); showMessage('Password updated. Please sign in.'); }
    } catch(err) { showMessage(err.message,true); }
    finally { $('#authSubmit').disabled=false; }
  });
  bootstrap();
})();
