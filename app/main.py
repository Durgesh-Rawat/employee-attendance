"""Employee Attendance & Analytics API (contract v2.0)."""
import os, re, math
from datetime import datetime, date, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal
from bson import ObjectId
from bson.json_util import dumps
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), serverSelectionTimeoutMS=1500)
db = client[os.getenv("MONGO_DB", "employee_attendance")]
IST = timezone(timedelta(hours=5, minutes=30)); UTC = timezone.utc
PRESENT = ["PRESENT", "WFH", "ON_DUTY"]
STATUSES = PRESENT + ["ABSENT", "LEAVE"]
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

@app.on_event("startup")
def indexes():
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="department_employee")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="department_joined")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_day")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="attendance_date_emp")
    db.attendance_logs.create_index([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], name="attendance_status_date")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="punchout_lookup")

def parse_date(s: str) -> date:
    try:
        d = date.fromisoformat(s)
        if d.isoformat() != s: raise ValueError()
        return d
    except Exception: raise HTTPException(422, "date must be YYYY-MM-DD")
def parse_month(s: str) -> tuple[date,date]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", s or ""): raise HTTPException(422, "month must be YYYY-MM")
    y,m=map(int,s.split('-')); start=date(y,m,1); return start, date(y+1,1,1) if m==12 else date(y,m+1,1)
def millis(v: Optional[int]) -> datetime:
    if v is None: dt=datetime.now(UTC)
    else:
        if type(v) is not int or not 100_000_000_000 <= v <= 4_102_444_800_000: raise HTTPException(422,"timestamp must be epoch milliseconds")
        dt=datetime.fromtimestamp(v/1000, UTC)
    return dt.replace(microsecond=0)
def utc(dt):
    if dt is None:return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
def epoch(dt): return int(utc(dt).timestamp()*1000) if dt else None
def local(dt): return utc(dt).astimezone(IST) if dt else None
def shift_end_dt(punch_in: datetime, shift_start: str, shift_end: str) -> datetime:
    li=local(punch_in); sh,sm=map(int,shift_start.split(':')); eh,em=map(int,shift_end.split(':'))
    start_day=li.date()
    if (eh,em) <= (sh,sm) and (li.hour,li.minute) < (eh,em): start_day -= timedelta(days=1)
    end_day=start_day + (timedelta(days=1) if (eh,em)<=(sh,sm) else timedelta())
    return datetime.combine(end_day,time(eh,em),IST).astimezone(UTC)
def attendance_date(ts, shift_start, shift_end):
    li=local(ts); sh,sm=map(int,shift_start.split(':')); eh,em=map(int,shift_end.split(':'))
    if (eh,em)<=(sh,sm) and (li.hour,li.minute)<(eh,em): return (li.date()-timedelta(days=1)).isoformat()
    return li.date().isoformat()
def late_minutes(ts, shift_start):
    li=local(ts); h,m=map(int,shift_start.split(':')); start=datetime.combine(li.date(),time(h,m),IST)
    delta=int((li-start).total_seconds()//60)
    return delta if (li-start).total_seconds()>600 else 0
def derived(emp, status, pin, pout):
    if status not in PRESENT: return {"late_minutes":0,"work_hours":None,"overtime_minutes":0,"half_day":False}
    late=late_minutes(pin,emp['shift_start']) if pin else 0
    if not pout or not pin: return {"late_minutes":late,"work_hours":None,"overtime_minutes":0,"half_day":False}
    seconds=(utc(pout)-utc(pin)).total_seconds()
    hours=float((Decimal(str(seconds/3600)).quantize(Decimal('0.01'),rounding=ROUND_HALF_UP)))
    end=shift_end_dt(pin,emp['shift_start'],emp['shift_end'])
    ot=int((utc(pout)-end).total_seconds()//60) if (utc(pout)-end).total_seconds()>=1800 else 0
    return {"late_minutes":late,"work_hours":hours,"overtime_minutes":max(0,ot),"half_day":hours<4.5}
def output_doc(d):
    d=dict(d); d.pop('_id',None)
    for k in ('punch_in','punch_out','created_at'):
        if k in d: d[k]=epoch(d[k])
    for h in d.get('history',[]) or []:
        if 'at' in h: h['at']=epoch(h['at'])
        for change in (h.get('changes') or {}).values():
            for k in ('from','to'):
                if isinstance(change.get(k),datetime): change[k]=epoch(change[k])
    return d
class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r'^EMP\d{4,6}$'); name: str=Field(min_length=1,max_length=100); email: str=Field(max_length=120,pattern=r'^[^@\s]+@[^@\s]+\.[^@\s]+$'); department: str=Field(min_length=1,max_length=50); shift_start: str=Field(default='09:30',pattern=r'^([01]\d|2[0-3]):[0-5]\d$'); shift_end: str=Field(default='18:30',pattern=r'^([01]\d|2[0-3]):[0-5]\d$'); joined_on: str
    @field_validator('joined_on') 
    @classmethod
    def valid_join(cls,v): 
         parse_date(v)
         return v
    @model_validator(mode='after')
    def shifts_differ(self):
        if self.shift_start==self.shift_end: raise ValueError('shift_start must differ from shift_end')
        return self
class PunchInIn(BaseModel):
    emp_code:str; punched_at:Optional[int]=None; status:Literal['PRESENT','WFH','ON_DUTY']='PRESENT'
    @field_validator('punched_at') 
    @classmethod
    def valid_ts(cls,v):
        if v is not None and (
                  type(v) is not int 
                  or not 100_000_000_000<=v<=4_102_444_800_000
        ): 
            raise ValueError('punched_at must be epoch milliseconds')
        return v
class PunchOutIn(BaseModel):
    emp_code:str; punched_at:Optional[int]=None
    @field_validator('punched_at') 
    @classmethod
    def valid_ts(cls,v):
        if v is not None and (
            type(v) is not int 
            or not 100_000_000_000<=v<=4_102_444_800_000
        ): 
            raise ValueError('punched_at must be epoch milliseconds')
        return v
class RegularizeIn(BaseModel):
    status:Optional[Literal['PRESENT','WFH','ON_DUTY','ABSENT','LEAVE']]=None; punch_in:Optional[int]=None; punch_out:Optional[int]=None; reason:str=Field(min_length=5,max_length=200); regularized_by:str=Field(min_length=1,max_length=50)
    @field_validator('punch_in','punch_out') 
    @classmethod
    def valid_ts(cls,v):
        if v is not None and (
            type(v) is not int 
            or not 100_000_000_000<=v<=4_102_444_800_000
        ): 
            raise ValueError('timestamp must be epoch milliseconds')
        return v

def page_args(page,page_size):
    if page<1 or not 1<=page_size<=100: raise HTTPException(422,'page must be >=1 and page_size between 1 and 100')
def pipeline_month(month):
    start,end=parse_month(month); return start.isoformat(),end.isoformat()
@app.get('/health')
def health():
    try: client.admin.command('ping'); return {'status':'ok'}
    except PyMongoError: raise HTTPException(503,'MongoDB unavailable')
@app.post('/employees',status_code=201)
def create_employee(body:EmployeeIn):
    doc=body.model_dump(); doc['created_at']=datetime.now(UTC).replace(microsecond=0)
    try: db.employees.insert_one(doc)
    except DuplicateKeyError: raise HTTPException(409,'emp_code already exists')
    return output_doc(doc)
@app.get('/employees')
def list_employees(department:Optional[str]=None,page:int=Query(1,ge=1),page_size:int=Query(20,ge=1,le=100)):
    q={'department':department} if department is not None else {}; total=db.employees.count_documents(q)
    items=[output_doc(x) for x in db.employees.find(q,{'_id':0}).sort('emp_code',ASCENDING).skip((page-1)*page_size).limit(page_size)]
    return {'items':items,'total':total,'page':page,'page_size':page_size}
@app.post('/attendance/punch-in',status_code=201)
def punch_in(body:PunchInIn):
    emp=db.employees.find_one({'emp_code':body.emp_code})
    if not emp: raise HTTPException(404,'unknown employee')
    ts=millis(body.punched_at); d=attendance_date(ts,emp['shift_start'],emp['shift_end'])
    doc={'emp_code':body.emp_code,'date':d,'status':body.status,'punch_in':ts,'punch_out':None,'work_hours':None,'late_minutes':late_minutes(ts,emp['shift_start']),'overtime_minutes':0,'half_day':False,'history':[]}
    try: db.attendance_logs.insert_one(doc)
    except DuplicateKeyError: raise HTTPException(409,'already punched in for this date')
    return output_doc(doc)
@app.post('/attendance/punch-out')
def punch_out(body:PunchOutIn):
    emp=db.employees.find_one({'emp_code':body.emp_code})
    if not emp: raise HTTPException(404,'unknown employee')
    ts=millis(body.punched_at)
    rec=db.attendance_logs.find_one({'emp_code':body.emp_code,'punch_in':{'$lte':ts},'status':{'$in':PRESENT}},sort=[('punch_in',DESCENDING)])
    if not rec: raise HTTPException(404,'no punch-in found')
    if rec.get('punch_out') is not None: raise HTTPException(409,'already punched out')
    if ts<=utc(rec['punch_in']) or (ts-utc(rec['punch_in'])).total_seconds()>86400: raise HTTPException(422,'punch-out must be after punch-in and within 24 hours')
    vals=derived(emp,rec['status'],rec['punch_in'],ts)
    updated=db.attendance_logs.find_one_and_update({'_id':rec['_id'],'punch_out':None},{'$set':{'punch_out':ts,**vals}},return_document=ReturnDocument.AFTER)
    if not updated: raise HTTPException(409,'already punched out')
    return output_doc(updated)
@app.get('/attendance')
def list_attendance(emp_code:Optional[str]=None,date_from:Optional[str]=None,date_to:Optional[str]=None,status:Optional[str]=None,page:int=Query(1,ge=1),page_size:int=Query(20,ge=1,le=100)):
    if date_from: parse_date(date_from)
    if date_to: parse_date(date_to)
    if date_from and date_to and date_from>date_to: raise HTTPException(422,'date_from must be <= date_to')
    q={}
    if emp_code:q['emp_code']=emp_code
    if status:
        if status not in STATUSES: raise HTTPException(422,'invalid status')
        q['status']=status
    if date_from or date_to:
        q['date']={}
        if date_from:q['date']['$gte']=date_from
        if date_to:q['date']['$lte']=date_to
    total=db.attendance_logs.count_documents(q); items=[output_doc(x) for x in db.attendance_logs.find(q,{'_id':0}).sort([('date',DESCENDING),('emp_code',ASCENDING)]).skip((page-1)*page_size).limit(page_size)]
    return {'items':items,'total':total,'page':page,'page_size':page_size}
@app.patch('/attendance/{emp_code}/{date}')
def regularize(emp_code:str,date:str,body:RegularizeIn):
    date_str=date; parse_date(date_str); emp=db.employees.find_one({'emp_code':emp_code})
    if not emp: raise HTTPException(404,'unknown employee')
    old=db.attendance_logs.find_one({'emp_code':emp_code,'date':date_str})
    if not old: raise HTTPException(404,'attendance record not found')
    status=body.status if body.status is not None else old['status']
    if status in ('ABSENT','LEAVE') and (body.punch_in is not None or body.punch_out is not None): raise HTTPException(422,'ABSENT/LEAVE cannot have punch times')
    pin=millis(body.punch_in) if body.punch_in is not None else old.get('punch_in'); pout=millis(body.punch_out) if body.punch_out is not None else old.get('punch_out')
    if status in PRESENT and pin is None: raise HTTPException(422,'presence status requires punch_in')
    if status not in PRESENT: pin=pout=None
    if pin and attendance_date(pin,emp['shift_start'],emp['shift_end'])!=date_str: raise HTTPException(422,'punch_in must fall on attendance date')
    if pout and (not pin or pout<=pin or (pout-pin).total_seconds()>86400): raise HTTPException(422,'invalid punch_out')
    vals=derived(emp,status,pin,pout); new={'status':status,'punch_in':pin,'punch_out':pout,**vals}
    tracked=['status','punch_in','punch_out','work_hours','late_minutes','overtime_minutes','half_day']; changes={}
    for k in tracked:
        before=old.get(k,False if k=='half_day' else 0 if k in ('late_minutes','overtime_minutes') else None)
        after=new[k]
        if before!=after: changes[k]={'from':before,'to':after}
    if not changes: raise HTTPException(422,'request makes no changes')
    hist={'at':datetime.now(UTC).replace(microsecond=0),'by':body.regularized_by,'reason':body.reason,'changes':changes}
    # Compare-and-swap prevents concurrent edits from overwriting history.
    updated=db.attendance_logs.find_one_and_update({'_id':old['_id'],'history':old.get('history',[]),'status':old['status'],'punch_in':old.get('punch_in'),'punch_out':old.get('punch_out')},{'$set':new,'$push':{'history':hist}},return_document=ReturnDocument.AFTER)
    if not updated: raise HTTPException(409,'record changed concurrently; retry')
    return output_doc(updated)
@app.get('/analytics/employees/{emp_code}/monthly')
def employee_monthly(emp_code:str,month:str):
    emp=db.employees.find_one({'emp_code':emp_code})
    if not emp: raise HTTPException(404,'unknown employee')
    start,end=pipeline_month(month); sdate=max(start,emp['joined_on']); days=max(0,(parse_date(end)-parse_date(sdate)).days)
    working=sum(1 for i in range(days) if (parse_date(sdate)+timedelta(days=i)).weekday()<5)
    pipe=[{'$match':{'emp_code':emp_code,'date':{'$gte':start,'$lt':end}}},{'$group':{'_id':None,'present_days':{'$sum':{'$cond':[{'$and':[{'$in':['$status',PRESENT]},{'$and':[{'$gte':[{'$isoDayOfWeek':{'$dateFromString':{'dateString':'$date'}}},1]},{'$lte':[{'$isoDayOfWeek':{'$dateFromString':{'dateString':'$date'}}},5]}]}]}, {'$cond':[{'$eq':[{'$ifNull':['$half_day',False]},True]},0.5,1]},0]}},'leave_days':{'$sum':{'$cond':[{'$eq':['$status','LEAVE']},1,0]}},'late_count':{'$sum':{'$cond':[{'$gt':[{'$ifNull':['$late_minutes',0]},0]},1,0]}},'total_late_minutes':{'$sum':{'$ifNull':['$late_minutes',0]}},'total_overtime_minutes':{'$sum':{'$ifNull':['$overtime_minutes',0]}}}}]
    row=list(db.attendance_logs.aggregate(pipe)); r=row[0] if row else {}
    present=float(r.get('present_days',0)); return {'emp_code':emp_code,'month':month,'working_days':working,'present_days':present,'leave_days':r.get('leave_days',0),'late_count':r.get('late_count',0),'total_late_minutes':r.get('total_late_minutes',0),'total_overtime_minutes':r.get('total_overtime_minutes',0),'attendance_pct':round_half(present/working*100,2) if working else None}
def round_half(v, places=2): return float(Decimal(str(v)).quantize(Decimal('1.'+'0'*places),rounding=ROUND_HALF_UP))
def month_match(month):
    start,end=pipeline_month(month); return {'date':{'$gte':start,'$lt':end}}
@app.get('/analytics/departments/summary')
def department_summary(month:str,department:Optional[str]=None):
    start,end=pipeline_month(month); match={'joined_on':{'$lt':end}}
    if department is not None:match['department']=department
    pipe=[{'$match':match},{'$lookup':{'from':'attendance_logs','let':{'code':'$emp_code','join':'$joined_on'},'pipeline':[{'$match':{'$expr':{'$and':[{'$eq':['$emp_code','$$code']},{'$gte':['$date',start]},{'$lt':['$date',end]},{'$gte':['$date','$$join']}]}}}], 'as':'logs'}},{'$unwind':{'path':'$logs','preserveNullAndEmptyArrays':True}},{'$group':{'_id':'$department','headcount':{'$addToSet':'$emp_code'},'present_days':{'$sum':{'$cond':[{'$in':['$logs.status',PRESENT]},{'$cond':[{'$eq':[{'$ifNull':['$logs.half_day',False]},True]},0.5,1]},0]}},'hours_sum':{'$sum':{'$cond':[{'$and':[{'$in':['$logs.status',PRESENT]},{'$ne':[{'$ifNull':['$logs.work_hours',None]},None]}]},'$logs.work_hours',0]}},'hours_count':{'$sum':{'$cond':[{'$and':[{'$in':['$logs.status',PRESENT]},{'$ne':[{'$ifNull':['$logs.work_hours',None]},None]}]},1,0]}},'late_count':{'$sum':{'$cond':[{'$gt':[{'$ifNull':['$logs.late_minutes',0]},0]},1,0]}},'total_late_minutes':{'$sum':{'$ifNull':['$logs.late_minutes',0]}},'leave_count':{'$sum':{'$cond':[{'$eq':['$logs.status','LEAVE']},1,0]}},'on_duty_count':{'$sum':{'$cond':[{'$eq':['$logs.status','ON_DUTY']},1,0]}}}},{'$project':{'_id':0,'department':'$_id','headcount':{'$size':'$headcount'},'present_days':1,'avg_work_hours':{'$cond':[{'$gt':['$hours_count',0]},{'$round':[{'$divide':['$hours_sum','$hours_count']},2]},None]},'late_count':1,'total_late_minutes':1,'leave_count':1,'on_duty_count':1}},{'$sort':{'department':1}}]
    return {'month':month,'items':list(db.employees.aggregate(pipe))}
@app.get('/analytics/leaderboard/late')
def late_leaderboard(month:str,limit:int=Query(10,ge=1,le=50),department:Optional[str]=None):
    pipe=[{'$match':month_match(month)},{'$group':{'_id':'$emp_code','total_late_minutes':{'$sum':{'$ifNull':['$late_minutes',0]}},'late_count':{'$sum':{'$cond':[{'$gt':[{'$ifNull':['$late_minutes',0]},0]},1,0]}}}},{'$match':{'total_late_minutes':{'$gt':0}}},{'$lookup':{'from':'employees','localField':'_id','foreignField':'emp_code','as':'employee'}},{'$unwind':'$employee'}]
    if department is not None:pipe.append({'$match':{'employee.department':department}})
    pipe += [{'$setWindowFields':{'sortBy':{'total_late_minutes':-1},'output':{'rank':{'$rank':{}}}}},{'$match':{'rank':{'$lte':limit}}},{'$sort':{'total_late_minutes':-1,'_id':1}},{'$project':{'_id':0,'emp_code':'$_id','name':'$employee.name','department':'$employee.department','total_late_minutes':1,'rank':1}}]
    return {'month':month,'items':list(db.attendance_logs.aggregate(pipe,allowDiskUse=True))}
@app.get('/analytics/departments/{department}/trend')
def department_trend(department:str,from_date:str=Query(...,alias='from'),to_date:str=Query(...,alias='to')):
    start=parse_date(from_date); end=parse_date(to_date)
    if end<start or (end-start).days+1>92: raise HTTPException(422,'invalid date range (maximum 92 days)')
    if not db.employees.find_one({'department':department}): raise HTTPException(404,'unknown department')
    # Daily aggregation is database-side; densify inserts calendar gaps, then windowFields computes the rolling mean.
    pipe=[{'$match':{'date':{'$gte':from_date,'$lte':to_date}}},{'$lookup':{'from':'employees','localField':'emp_code','foreignField':'emp_code','as':'emp'}},{'$unwind':'$emp'},{'$match':{'emp.department':department}},{'$match':{'$expr':{'$gte':['$date','$emp.joined_on']}}},{'$group':{'_id':'$date','present_count':{'$sum':{'$cond':[{'$in':['$status',PRESENT]},{'$cond':[{'$eq':[{'$ifNull':['$half_day',False]},True]},0.5,1]},0]}},'late_count':{'$sum':{'$cond':[{'$gt':[{'$ifNull':['$late_minutes',0]},0]},1,0]}}}},{'$project':{'_id':0,'day':{'$dateFromString':{'dateString':'$_id'}},'present_count':1,'late_count':1}},{'$densify':{'field':'day','range':{'step':1,'unit':'day','bounds':[datetime.combine(start,time(),UTC),datetime.combine(end,time(),UTC)]}}},{'$set':{'daystr':{'$dateToString':{'date':'$day','format':'%Y-%m-%d'}},'present_count':{'$ifNull':['$present_count',0]},'late_count':{'$ifNull':['$late_count',0]},'weekday':{'$isoDayOfWeek':'$day'}}},{'$lookup':{'from':'employees','let':{'d':'$daystr'},'pipeline':[{'$match':{'department':department}},{'$match':{'$expr':{'$lte':['$joined_on','$$d']}}},{'$count':'n'}],'as':'hc'}},{'$set':{'headcount':{'$ifNull':[{'$arrayElemAt':['$hc.n',0]},0]},'is_working_day':{'$lte':['$weekday',5]} }},{'$set':{'attendance_rate':{'$cond':[{'$and':['$is_working_day',{'$gt':['$headcount',0]}]},{'$round':[{'$divide':['$present_count','$headcount']},4]},None]}}},{'$setWindowFields':{'sortBy':{'day':1},'output':{'moving_avg_7d':{'$avg':'$attendance_rate','window':{'documents':[-6,0]}}}}},{'$project':{'_id':0,'date':'$daystr','is_working_day':1,'headcount':1,'present_count':1,'late_count':1,'attendance_rate':1,'moving_avg_7d':{'$cond':[{'$eq':[{'$type':'$moving_avg_7d'},'missing']},None,{'$round':['$moving_avg_7d',4]}]}}},{'$sort':{'date':1}}]
    return {'department':department,'items':list(db.attendance_logs.aggregate(pipe,allowDiskUse=True))}
@app.get('/admin/explain/{endpoint}')
def explain(endpoint:str,emp_code:Optional[str]=None,month:Optional[str]=None,department:Optional[str]=None,limit:int=Query(10,ge=1,le=50),date_from:Optional[str]=None,date_to:Optional[str]=None,status:Optional[str]=None,from_date:Optional[str]=Query(None,alias='from'),to_date:Optional[str]=Query(None,alias='to'),page:int=Query(1,ge=1),page_size:int=Query(20,ge=1,le=100)):
    if endpoint=='attendance_list':
        q={}
        if emp_code:q['emp_code']=emp_code
        if status:q['status']=status
        if date_from or date_to:
            q['date']={}
            if date_from:q['date']['$gte']=date_from
            if date_to:q['date']['$lte']=date_to
        cmd={'find':'attendance_logs','filter':q,'sort':{'date':-1,'emp_code':1},'skip':(page-1)*page_size,'limit':page_size}
        collection='attendance_logs'
    elif endpoint=='employee_monthly':
        if not emp_code or not month:raise HTTPException(422,'emp_code and month are required')
        cmd={'aggregate':'attendance_logs','pipeline':[{'$match':{'emp_code':emp_code,**month_match(month)}},{'$group':{'_id':'$emp_code','late_count':{'$sum':{'$cond':[{'$gt':['$late_minutes',0]},1,0]}}}}],'cursor':{}}; collection='attendance_logs'
    elif endpoint=='department_summary':
        if not month:raise HTTPException(422,'month is required')
        cmd={'aggregate':'employees','pipeline':[{'$match':{'joined_on':{'$lt':pipeline_month(month)[1]},**({'department':department} if department else {})}},{'$lookup':{'from':'attendance_logs','localField':'emp_code','foreignField':'emp_code','as':'logs'}}],'cursor':{}}; collection='employees'
    elif endpoint=='late_leaderboard':
        if not month:raise HTTPException(422,'month is required')
        cmd={'aggregate':'attendance_logs','pipeline':[{'$match':month_match(month)},{'$group':{'_id':'$emp_code','total_late_minutes':{'$sum':'$late_minutes'}}}],'cursor':{}}; collection='attendance_logs'
    elif endpoint=='department_trend':
        if not department or not from_date or not to_date:raise HTTPException(422,'department, from and to are required')
        parse_date(from_date);parse_date(to_date)
        cmd={'aggregate':'attendance_logs','pipeline':[{'$match':{'date':{'$gte':from_date,'$lte':to_date}}},{'$lookup':{'from':'employees','localField':'emp_code','foreignField':'emp_code','as':'emp'}}],'cursor':{}}; collection='attendance_logs'
    else: raise HTTPException(422,'unknown endpoint')
    try: result=db.command('explain',cmd,verbosity='executionStats')
    except Exception as e: raise HTTPException(422,f'could not explain query: {e}')
    return {'endpoint':endpoint,'collection':collection,'explain':jsonable_encoder(result,custom_encoder={ObjectId:str,datetime:epoch})}
