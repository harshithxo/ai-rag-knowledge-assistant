from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr, Field
from app.config import settings
from app.errors import ValidationFailed
from app.services import auth_service as auth

router=APIRouter(prefix="/auth",tags=["authentication"])
class RegisterBody(BaseModel):
    full_name:str=Field(min_length=2,max_length=100); email:EmailStr; password:str=Field(min_length=10,max_length=128)
class LoginBody(BaseModel): email:EmailStr; password:str
class EmailBody(BaseModel): email:EmailStr
class TokenBody(BaseModel): token:str=Field(min_length=20,max_length=256)
class ResetBody(TokenBody): password:str=Field(min_length=10,max_length=128); confirm_password:str
class ProfileBody(BaseModel): full_name:str=Field(min_length=2,max_length=100)
class ChangePasswordBody(BaseModel): current_password:str; new_password:str=Field(min_length=10,max_length=128); confirm_password:str

def _cookie(response:Response, token:str, csrf:str):
    secure=settings.app_env.lower()=="production"
    response.set_cookie("docmind_session",token,httponly=True,secure=secure,samesite="strict",max_age=settings.session_expire_minutes*60,path="/")
    response.set_cookie("docmind_csrf",csrf,httponly=False,secure=secure,samesite="strict",max_age=settings.session_expire_minutes*60,path="/")

def get_user(request:Request):
    user=auth.current_user(request.cookies.get("docmind_session"))
    if not user: raise HTTPException(401,"Authentication required")
    return user

def csrf_guard(request:Request,user=Depends(get_user),x_csrf_token:str|None=Header(default=None)):
    if request.method not in ("GET","HEAD","OPTIONS") and not auth.csrf_valid(user,x_csrf_token):
        raise HTTPException(403,"CSRF validation failed")
    return user

@router.post("/register",status_code=201)
def register(body:RegisterBody):
    auth.register(body.full_name,body.email,body.password)
    return {"message":"Account created. Check your email for a verification link."}
@router.post("/verify-email")
def verify(body:TokenBody): auth.verify_email(body.token); return {"message":"Email verified. You can now sign in."}
@router.post("/resend-verification")
def resend(body:EmailBody): auth.resend_verification(body.email); return {"message":"If the account needs verification, an email has been sent."}
@router.post("/login")
def login(body:LoginBody,response:Response):
    user,token,csrf=auth.login(body.email,body.password); _cookie(response,token,csrf); return {"user":user,"csrf_token":csrf}
@router.post("/logout")
def logout(request:Request,response:Response,user=Depends(csrf_guard)):
    auth.logout(request.cookies.get("docmind_session")); response.delete_cookie("docmind_session",path="/"); response.delete_cookie("docmind_csrf",path="/"); return {"success":True}
@router.get("/me")
def me(user=Depends(get_user)): return {"user":auth.profile(user["id"])}
@router.post("/forgot-password")
def forgot(body:EmailBody): auth.forgot_password(body.email); return {"message":"If an account exists, a reset link has been sent."}
@router.post("/reset-password")
def reset(body:ResetBody):
    if body.password!=body.confirm_password: raise ValidationFailed("Passwords do not match")
    auth.reset_password(body.token,body.password); return {"message":"Password updated. Please sign in again."}
@router.patch("/profile")
def profile(body:ProfileBody,user=Depends(csrf_guard)): return {"user":auth.update_profile(user["id"],body.full_name)}

@router.post("/change-password")
def change_password(body:ChangePasswordBody,user=Depends(csrf_guard)):
    if body.new_password != body.confirm_password: raise ValidationFailed("Passwords do not match")
    auth.change_password(user["id"], body.current_password, body.new_password)
    return {"message":"Password changed. Please sign in again."}
