#!/bin/bash

check_and_modify_fw_config() {

  set -e
  target="root@$1"

  logit() {
    echo "$@"
  }

  model="$(ssh ${target} cros_config / name)"
  rev="$(ssh ${target} ectool cbi get 0 | grep 'As uint:' | cut -d' ' -f3)"
  sku="$(ssh ${target} ectool cbi get 2 | grep 'As uint:' | cut -d' ' -f3)"
  fw_config="$(ssh ${target} ectool cbi get 6 | grep 'As uint:' | cut -d' ' -f3)"
  fw_type="$(ssh ${target} crossystem mainfw_type)"
  wpsw_cur="$(ssh ${target} crossystem wpsw_cur)"
  new_fw_config=0

  # only apply to the normal mode devices and which fw_config is zero
  # except copano and drobit
  if [ "${fw_type}" != "normal" ] || \
     [ "${fw_config}" -ne 0 -a \
       "${model}" != "copano" -a \
       "${model}" != "drobit" ]; then
    logit "Exit 1"
    exit 0
  fi

  # only apply to the board revision <= 2
  if [ "${rev}" -gt 2 ]; then
    exit 0
  fi

  # apply to copano, delbin, drobit
  # eldrid, elemi, lillipup, lindar,
  # voema, volteer2 and voxel
  case ${model} in
    copano)
      if [ "${rev}" -eq 1 ]; then
        case ${sku} in
          917505)
            new_fw_config=8604675
            ;;
          917506)
            new_fw_config=42159107
            ;;
          *)
            exit 0
            ;;
        esac
      else
        exit 0
      fi
      ;;
    delbin)
      if [ "${rev}" -eq 2 -a "${wpsw_cur}" -eq 0 ]; then
        case ${sku} in
          65540)
            new_fw_config=8456706
            ;;
          *)
            exit 0
            ;;
        esac
      else
        exit 0
      fi
      ;;
    delbin_npcx796fc)
      if [ "${rev}" -eq 1 -a "${wpsw_cur}" -eq 0 ]; then
        case ${sku} in
          65537|65538|65539)
            new_fw_config=68098
            ;;
          *)
            exit 0
            ;;
        esac
      else
        exit 0
      fi
      ;;
    drobit)
      if [ "${rev}" -eq 1 ]; then
        case ${sku} in
          786433|786434|786435)
            new_fw_config=8471043
            ;;
          *)
            exit 0
            ;;
        esac
      else
        exit 0
      fi
      ;;
    eldrid)
      if [ "${rev}" -eq 2 -a "${wpsw_cur}" -eq 0 ]; then
        case ${sku} in
          589841)
            new_fw_config=8670466
            ;;
          589842)
            new_fw_config=9719042
            ;;
          *)
            exit 0
            ;;
        esac
      else
        exit 0
      fi
      ;;
    eldrid_npcx796)
      if [ "${rev}" -eq 1 -a "${wpsw_cur}" -eq 0 ]; then
        case ${sku} in
          589825|589826)
            new_fw_config=8670466
            ;;
          *)
            exit 0
            ;;
        esac
      else
        exit 0
      fi
      ;;
    elemi)
      if [ "${rev}" -le 2 -a "${wpsw_cur}" -eq 0 ]; then
        case ${sku} in
          720897|720898|720900|720901|720912|720913|720914|720916|\
	  720928|720931|720933)
            new_fw_config=4474114
            ;;
          720915|720929)
            new_fw_config=4457730
            ;;
          720917|720918|720919|720935)
            new_fw_config=8652034
            ;;
          720899|720902|720930|720932|720934)
            new_fw_config=8668418
            ;;
          *)
            exit 0
            ;;
        esac
      else
        exit 0
      fi
      ;;
    lillipup)
      if [ "${rev}" -eq 1 -a "${wpsw_cur}" -eq 0 ]; then
        case ${sku} in
          524289)
            new_fw_config=75845125
            ;;
          524290)
            new_fw_config=75828741
            ;;
          524291)
            new_fw_config=71634437
            ;;
          524292)
            new_fw_config=71650821
            ;;
          *)
            exit 0
            ;;
        esac
      else
        exit 0
      fi
      ;;
    lindar)
      if [ "${rev}" -eq 1 -a "${wpsw_cur}" -eq 0 ]; then
        case ${sku} in
          458753)
            new_fw_config=8717829
            ;;
          458754|458755)
            new_fw_config=8734213
            ;;
          *)
            exit 0
            ;;
        esac
      else
        exit 0
      fi
      ;;
    voema)
      if [ "${rev}" -eq 1 -a "${wpsw_cur}" -eq 0 ]; then
        case ${sku} in
          851969|851970|851985|851986|851987)
            new_fw_config=8408066
            ;;
          *)
            exit 0
            ;;
        esac
      else
        exit 0
      fi
      ;;
    volta)
      if [ "${rev}" -le 2 -a "${wpsw_cur}" -eq 0 ]; then
        case ${sku} in
          393233|393235|393237|393239|393241|393243|393245|393247)
            new_fw_config=8602115
            ;;
          393234|393236|393238|393240|393242|393244|393246|393248)
            new_fw_config=8585731
            ;;
          *)
            exit 0
            ;;
        esac
      else
        exit 0
      fi
      ;;
    volteer)
      if [ "${rev}" -eq 1 -a "${wpsw_cur}" -eq 0 ]; then
        case ${sku} in
          1)
            new_fw_config=16861442
            ;;
          2)
            new_fw_config=8472833
            ;;
          3|5)
            new_fw_config=8542979
            ;;
          4)
            new_fw_config=8542721
            ;;
          *)
            exit 0
            ;;
        esac
      else
        exit 0
      fi
      ;;
    volteer2)
      if [ "${rev}" -eq 2 -a "${wpsw_cur}" -eq 0 ]; then
        case ${sku} in
          6|10)
            new_fw_config=16861442
            ;;
          7)
            new_fw_config=8473089
            ;;
          8)
            new_fw_config=8473091
            ;;
          9|11)
            new_fw_config=8477443
            ;;
          *)
            exit 0
            ;;
        esac
      else
        exit 0
      fi
      ;;
    voxel)
      if [ "${rev}" -le 2 -a "${wpsw_cur}" -eq 0 ]; then
        case ${sku} in
          39321[7-9]|39322[0-8])
            new_fw_config=8604163
            ;;
          *)
            exit 0
            ;;
        esac
      else
        exit 0
      fi
      ;;
    *)
      exit 0
      ;;
  esac

  # if the fw_config is the same as new_fw_config or
  # the new_fw_config is zero , skip it
  if [ "${fw_config}" -eq "${new_fw_config}" -o \
       "${new_fw_config}" -eq 0 ]; then
    exit 0
  fi

  # apply the fw_config
  ret=$(ssh ${target} ectool cbi set 6 "${new_fw_config}" 4)
  if [ -n "${ret}" ]; then
    logit "Failed to override fw_config to ${new_fw_config} for sku ${sku}"
    exit 0
  fi

  logit "Override fw_config to ${new_fw_config} for sku ${sku}"
}

check_and_modify_fw_config "$@"
