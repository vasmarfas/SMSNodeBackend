# GoIP UDP Command Map

Источник: `gsm-gateway-spec/goip/goip_sms_Interface_en.txt` и PDF-версия.

## Registration / Keepalive
- `req:$count;id:$id;pass:$password;...` -> `reg:$count;status:0;`

## Send SMS Flow
- `MSG $sendid $length $msg`
- `PASSWORD $sendid $password`
- `SEND $sendid $telid $telnum`
- Ответы: `OK $sendid $telid`, `WAIT $sendid $telid`, `ERROR $sendid ...`
- Завершение: `DONE $sendid` -> `DONE $sendid`

## Receive SMS
- `RECEIVE:$recvid;id:$id;password:$password;srcnum:$srcnum;msg:$msg`
- ACK: `RECEIVE $recvid OK`

## Channel / Device Control
- `get_gsm_num`, `set_gsm_num`
- `get_exp_time`, `set_exp_time`
- `get_remain_time`, `reset_remain_time`
- `get_gsm_state`
- `svr_drop_call`
- `svr_reboot_module`
- `svr_reboot_dev`
- `CF ...` -> `CFOK|CFERROR`

## Push Status from GoIP
- `STATE:$recvid;id:$goipid;password:$password;gsm_remain_state:$state` -> `STATE $recvid OK`
- `RECORD:$recvid;id:$goipid;password:$password;dir:$dir;num:$num` -> `RECORD $recvid OK`
- `REMAIN:$recvid;id:$goipid;password:$password;gsm_remain_time:$time` -> `REMAIN $recvid OK`

## USSD
- `USSD $sendid $password $ussdcmd` -> `USSD $sendid $msg` | `USSDERROR ...`
- `USSDEXIT $sendid $password` -> `USSDEXIT $sendid`

## IMEI
- `get_imei $sendid $password`
- `set_imei $sendid $imei $password`

## Out Call Interval
- `get_out_call_interval $sendid $password`
- `set_out_call_interval $sendid $interval $password`

## Module Control
- `module_ctl_i $sendid $value $password`
- `module_ctl $sendid $value $password`

## Cells
- Push: `CELLS:$recvid;id:$id;password:$password;lists:$cell_list` -> `CELLS $recvid OK`
- `set_base_cell $sendid $cell_id $password`
- `get_cells_list $sendid $password` (далее приходит push `CELLS`)
- `CURCELL $sendid $password`
