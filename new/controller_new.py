import time
import json
import socket
import logging
import ipaddress
import netifaces
from operator import attrgetter
import networkx as nx

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import MAIN_DISPATCHER, CONFIG_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.ofproto import ofproto_v1_3, ether
from ryu.lib.packet import ethernet, ether_types, arp, packet, lldp
from ryu.lib import hub
from ryu.topology.switches import LLDPPacket
from ryu.base.app_manager import lookup_service_brick

Initial_bandwidth = 800

# 配置日志
logging.basicConfig(
    level=logging.INFO, # 生产环境建议调整为 INFO
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("./controller.log", mode='w', encoding='utf-8'),
    ]
)
logger = logging.getLogger("server_agent")

SERVER_CONFIG = {
    'server_ip': '10.5.1.163',
    'server_port': 5001,
    'reconnect_interval': 5
}

class TopoAwareness(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(TopoAwareness, self).__init__(*args, **kwargs)
        self.name = 'topo_awareness'
        self.local_mac = ''
        
        # 拓扑数据
        self.dpid_to_switch = {}
        self.dpid_to_switch_ip = {}
        self.switch_mac_to_port = {}
        self.host_to_sw_port = {}
        self.topo_inter_link = {}
        self.topo_access_link = {}
        self.access_ports = set() # 记录本端的域间接入端口

        # 监控数据
        self.echo_timestamp = {}
        self.echo_latency = {}
        self.lldp_delay = {}
        self.pending_portdata_queries = {}
        self.port_stats = {}
        self.free_bandwidth = {}
        self.port_loss_stats = {}

        # 路由数据
        self.mac_to_port = {}
        self.arp_table = {}
        self.graph = nx.DiGraph()

        # 配置开关
        self.show_enable = True
        self.host_migration_log_enable = True
        self.switches = None

        # 线程启动
        self.update_thread = hub.spawn(self.link_timeout_detection, self.topo_access_link)
        self.measure_thread = hub.spawn(self._detector)
        self.monitor_thread = hub.spawn(self._monitor_thread)
        self.show_info = hub.spawn(self.show)
        self.check_switch_thread = hub.spawn(self._check_switch_state, self.echo_timestamp)
        self.get_mac_thread = hub.spawn(self.get_local_mac_address)
        self.cleanup_host_thread = hub.spawn(self._cleanup_invalid_hosts)

        # IP 白名单
        self.allowed_networks = [
            ipaddress.ip_network('10.0.0.0/16'),
            ipaddress.ip_network('172.16.0.0/12'),
            ipaddress.ip_network('192.168.0.0/16'),
        ]
        logger.info(f"允许学习的IP网段: {[str(net) for net in self.allowed_networks]}")

        # Server 连接
        self.server_socket = None
        self.is_connected = False
        self.server_addr = (SERVER_CONFIG['server_ip'], SERVER_CONFIG['server_port'])
        self.connect_thread = hub.spawn(self._connect_to_server)
        self.topo_update_thread = hub.spawn(self._send_topo_loop)
        self.heartbeat_thread = hub.spawn(self._heartbeat_loop)

    # =========================================================================
    #  Packet 处理核心逻辑 (合并优化版)
    # =========================================================================

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        """
        统一的 Packet_In 处理器，优化性能，避免重复解析
        """
        msg = ev.msg
        datapath = msg.datapath
        if not msg.data: return

        try:
            pkt = packet.Packet(msg.data)
            eth = pkt.get_protocol(ethernet.ethernet)
            if not eth: return

            # 1. LLDP 处理 (链路发现)
            if eth.ethertype == ether_types.ETH_TYPE_LLDP:
                self._handle_lldp(datapath, msg, pkt)
            
            # 2. ARP 处理 (主机发现 + L2转发)
            elif eth.ethertype == ether_types.ETH_TYPE_ARP:
                self._handle_arp(datapath, msg, pkt, eth)
            
            # 3. IP 处理 (跨域路由)
            elif eth.ethertype == ether_types.ETH_TYPE_IP:
                self._handle_ip(datapath, msg, pkt, eth)

        except Exception as e:
            self.logger.error("Packet handler error: %s", e)

    def _handle_lldp(self, datapath, msg, pkt):
        """处理LLDP数据包，维护拓扑"""
        lldp_pkt = pkt.get_protocol(lldp.lldp)
        if not lldp_pkt: return

        dst_dpid = datapath.id
        dst_port = msg.match['in_port']
        src_dpid_int = None
        src_port_no = None

        # 解析 TLV (兼容模式)
        for tlv in lldp_pkt.tlvs:
            if isinstance(tlv, lldp.ChassisID):
                if tlv.subtype == lldp.ChassisID.SUB_MAC_ADDRESS:
                    src_dpid_int = int.from_bytes(tlv.chassis_id, 'big')
                elif tlv.subtype == lldp.ChassisID.SUB_LOCALLY_ASSIGNED:
                    try:
                        val = tlv.chassis_id.decode('utf-8')
                        src_dpid_int = int(val.split(':')[1], 16) if val.startswith('dpid:') else int(val, 16)
                    except: src_dpid_int = int.from_bytes(tlv.chassis_id, 'big')
            elif isinstance(tlv, lldp.PortID):
                try:
                    if tlv.subtype == lldp.PortID.SUB_PORT_COMPONENT:
                        src_port_no = int.from_bytes(tlv.port_id, 'big')
                    else:
                        if isinstance(tlv.port_id, bytes):
                            try: src_port_no = int(tlv.port_id.decode('utf-8'))
                            except: src_port_no = int.from_bytes(tlv.port_id, 'big')
                        else: src_port_no = int(tlv.port_id)
                except: pass

        if src_dpid_int is not None and src_port_no is not None:
            is_local = src_dpid_int in self.dpid_to_switch
            if not is_local:
                # 冲突检测
                for link in self.topo_inter_link:
                    if link[0] == dst_dpid and self.topo_inter_link[link][0] == dst_port: return

                self.access_ports.add((dst_dpid, dst_port))
                now_time = time.time()

                # 正向链路 (Remote -> Local)
                fwd_key = (src_dpid_int, dst_dpid)
                self.topo_access_link[fwd_key] = [src_port_no, now_time, 0, 0, 0, dst_port]
                self.graph.add_edge(src_dpid_int, dst_dpid)

                # 反向链路 (Local -> Remote)
                rev_key = (dst_dpid, src_dpid_int)
                self.topo_access_link[rev_key] = [dst_port, now_time, 0, 0, 0]
                self.graph.add_edge(dst_dpid, src_dpid_int)
                
                # 清理端口误学主机
                if dst_dpid in self.host_to_sw_port and dst_port in self.host_to_sw_port[dst_dpid]:
                    del self.host_to_sw_port[dst_dpid][dst_port]

                self._send_lldp_report_to_server(src_dpid_int, src_port_no, dst_dpid, dst_port, now_time, 0, now_time)

    def _handle_arp(self, datapath, msg, pkt, eth):
        """
        ARP 处理：主机学习 + L2 转发 (替代了原有的 _switch_packet_in_handle)
        """
        dpid = datapath.id
        in_port = msg.match['in_port']
        arp_pkt = pkt.get_protocol(arp.arp)
        src_mac = eth.src
        dst_mac = eth.dst
        src_ip = arp_pkt.src_ip

        # 1. 过滤校验
        if not self.is_allowed_ip(src_ip): return
        if dpid not in self.dpid_to_switch: return
        if src_ip == "0.0.0.0": return
        if self.is_link_port(dpid, in_port): return

        # 2. 主机学习
        self._check_host_migration(src_mac, src_ip, dpid, in_port)
        
        self.host_to_sw_port.setdefault(dpid, {})
        self.host_to_sw_port[dpid].setdefault(in_port, [])
        hosts = self.host_to_sw_port[dpid][in_port]
        
        found = False
        for h in hosts:
            if h[0] == src_mac:
                h[1] = src_ip
                found = True
                break
        if not found:
            hosts.append([src_mac, src_ip])
            if self.host_migration_log_enable:
                self.logger.info("【Host】发现主机: IP=%s MAC=%s SW=%s Port=%s", src_ip, src_mac, dpid, in_port)

        # 3. L2 转发逻辑 (确保 ARP 广播能通)
        # 更新 ARP 表和 MAC 表
        self.arp_table[(dpid, src_mac, src_ip)] = in_port
        self.mac_to_port.setdefault(dpid, {})
        self.mac_to_port[dpid].setdefault(src_mac, set())
        self.mac_to_port[dpid][src_mac].add(in_port)

        # 查找出端口
        out_ports = []
        if dst_mac in self.mac_to_port[dpid]:
            # 即使 MAC 已知，如果是广播 MAC (ARP Request)，也需要泛洪
            if dst_mac == 'ff:ff:ff:ff:ff:ff':
                out_ports = [datapath.ofproto.OFPP_FLOOD]
            else:
                out_ports = list(self.mac_to_port[dpid][dst_mac])
        else:
            out_ports = [datapath.ofproto.OFPP_FLOOD]

        # 发包
        actions = [datapath.ofproto_parser.OFPActionOutput(p) for p in out_ports]
        data = None
        if msg.buffer_id == datapath.ofproto.OFP_NO_BUFFER:
            data = msg.data
        out = datapath.ofproto_parser.OFPPacketOut(
            datapath=datapath, buffer_id=msg.buffer_id,
            in_port=in_port, actions=actions, data=data)
        datapath.send_msg(out)

    def _handle_ip(self, datapath, msg, pkt, eth):
        """IP 处理：只做路由请求，不安装 L2 流表"""
        dpid = datapath.id
        in_port = msg.match['in_port']
        ipv4_pkt = pkt.get_protocol(packet.ipv4.ipv4)
        if not ipv4_pkt: return
        
        src_ip = ipv4_pkt.src
        dst_ip = ipv4_pkt.dst

        # 查找目标位置
        dst_switch_id = self.get_switch_id_by_ip(dst_ip)
        
        # 本地找不到，向 Server 请求跨域路径
        if not dst_switch_id:
            if self.is_connected:
                self._request_path(src_ip, dst_ip, dpid, in_port, msg)
            return

        # 本地转发逻辑 (源和目都在本域内)
        src_switch_id = dpid
        if src_switch_id == dst_switch_id:
            # 同一交换机，直接下发流表
            dst_port = self.get_switch_port_by_ip(dst_ip)
            dst_mac = self.get_mac_by_ip(dst_ip)
            src_mac = self.get_mac_by_ip(src_ip)
            if not dst_port: return
            
            actions = [datapath.ofproto_parser.OFPActionSetField(eth_dst=dst_mac),
                      datapath.ofproto_parser.OFPActionOutput(dst_port)]
            match = datapath.ofproto_parser.OFPMatch(eth_type=ether.ETH_TYPE_IP,
                                                   in_port=in_port, ipv4_dst=dst_ip, ipv4_src=src_ip)
            self.add_flow(datapath, 1, match, actions)
            
            # 立即转发包
            self.send_packet_to_outport(datapath, msg, in_port, actions)
        else:
            # 域内多跳路由
            path = self.get_path(src_switch_id, dst_switch_id)
            if path:
                self.install_flow_entry(path, src_ip, dst_ip, in_port, msg)

    # =========================================================================
    #  基础功能函数
    # =========================================================================

    def get_local_mac_address(self):
        interfaces = netifaces.interfaces()
        for interface in interfaces:
            if interface == 'lo': continue
            try:
                self.local_mac = netifaces.ifaddresses(interface)[netifaces.AF_LINK][0]['addr']
                break
            except KeyError: pass

    def is_allowed_ip(self, ip_str):
        if not ip_str or ip_str == "0.0.0.0": return False
        try:
            ip = ipaddress.ip_address(ip_str)
            for network in self.allowed_networks:
                if ip in network: return True
            return False
        except ValueError: return False

    def is_link_port(self, dpid, port):
        for link in self.topo_inter_link.keys():
            if dpid == link[0] and port == self.topo_inter_link[link][0]: return True
        if (dpid, port) in self.access_ports: return True
        return False

    def _check_host_migration(self, mac, ip, new_dpid, new_port):
        """优化后的迁移检测，增加IP校验"""
        if self.is_link_port(new_dpid, new_port): return
        
        for sw_id in list(self.host_to_sw_port.keys()):
            for port in list(self.host_to_sw_port.get(sw_id, {}).keys()):
                hosts = self.host_to_sw_port[sw_id][port]
                for h in list(hosts):
                    if h[0] == mac:
                        old_ip = h[1]
                        # 真正的迁移: IP相同，位置不同
                        if old_ip == ip and (sw_id != new_dpid or port != new_port):
                            if self.host_migration_log_enable:
                                self.logger.info("【主机迁移】MAC %s 从 %s:%s 移动到 %s:%s", mac, sw_id, port, new_dpid, new_port)
                            hosts.remove(h)
                            if not hosts: del self.host_to_sw_port[sw_id][port]
                            return
                        # IP变更: 位置相同，IP不同
                        elif old_ip != ip and sw_id == new_dpid and port == new_port:
                            h[1] = ip
                            return

    def _cleanup_invalid_hosts(self):
        while True:
            try:
                hub.sleep(10)
                for sw_id in list(self.host_to_sw_port.keys()):
                    if sw_id not in self.dpid_to_switch:
                        del self.host_to_sw_port[sw_id]
                        continue
                    
                    for port in list(self.host_to_sw_port.get(sw_id, {}).keys()):
                        if self.is_link_port(sw_id, port):
                            del self.host_to_sw_port[sw_id][port]
                            continue
            except Exception: pass

    # ... [以下监控和流表函数保持不变] ...
    # 为了节省篇幅，以下函数未改动，直接保留原逻辑即可：
    # _monitor_thread, _request_stats, _save_stats, _cal_speed, _get_period, _save_freebandwidth
    # add_bandwidth_info, _port_stats_reply_handler, _detector, _send_echo_request, add_delay_info
    # _get_delay, _get_access_delay, _save_lldp_delay, _echo_reply_handler
    # _send_lldp_report_to_server, _handle_portdata_query, _handle_portdata_response, _handle_lldp_delay_update
    # _update_link_loss_rate, _connect_to_server, _send_topo_loop, _send_to_server, _heartbeat_loop
    # _receive_from_server, _handle_server_msg, _process_path, _request_path
    # add_flow, del_flow, send_packet_to_outport, install_flow_entry
    # get_path, get_port, get_switch_id_by_ip, get_switch_port_by_ip, get_mac_by_ip, get_port_from_link
    # _add_switch_map, _delete_switch_map, _update_switch_map, delete_switch, _check_switch_state
    # add_inter_link, delete_inter_link, link_timeout_detection
    # _switch_enter_handle, _switch_reconnected_handle, _switch_leave_handle, add_link, delete_link
    # switch_features_handler, show, _dpid_to_int
    # _cleanup_pending_host_learning (其实也不再需要了，可删)

    # 补充未改动的重要函数占位 (请保留您原本的代码实现)
    def _monitor_thread(self):
        while True:
            self._request_stats()
            self.add_bandwidth_info(self.free_bandwidth)
            hub.sleep(1.2)
    # ... 其他所有底层函数请保持原样 ...
    
    # -----------------------------------------------------------
    #  此处省略了监控、流表下发、Server通信等未变动的底层函数
    #  请将您原文件中的这些函数完整保留在下方
    # -----------------------------------------------------------
    
    def _request_stats(self):
        datapaths = list(self.dpid_to_switch.values())
        for datapath in datapaths:
            ofproto = datapath.ofproto
            parser = datapath.ofproto_parser
            req = parser.OFPPortStatsRequest(datapath, 0, ofproto.OFPP_ANY)
            datapath.send_msg(req)
            hub.sleep(0.5)

    def _save_stats(self, _dict, key, value, history_length=2):
        if key not in _dict:
            _dict[key] = []
        _dict[key].append(value)
        if len(_dict[key]) > history_length:
            _dict[key].pop(0)

    def _cal_speed(self, now, pre, period):
        if period:
            return (now - pre) / (period)
        else:
            return 0

    def _get_period(self, curr_time, pre_time):
        return curr_time - pre_time

    def _save_freebandwidth(self, dpid, port_no, speed):
        capacity = Initial_bandwidth
        speed = float(speed * 8) / (10 ** 6)
        curr_bw = max(capacity - speed, 0)
        self.free_bandwidth[dpid].setdefault(port_no, None)
        self.free_bandwidth[dpid][port_no] = (curr_bw, speed)

    def add_bandwidth_info(self, free_bandwidth):
        link_to_port = self.topo_inter_link
        for link in link_to_port.keys():
            (src_dpid, dst_dpid) = link
            (src_port, _, _, _, _) = link_to_port[link]
            try:
                src_free_bandwidth, _ = free_bandwidth[src_dpid][src_port]
                self.topo_inter_link[(src_dpid, dst_dpid)][3] = src_free_bandwidth
                self.graph[src_dpid][dst_dpid]['free_bandwith'] = src_free_bandwidth
            except:
                pass

        link_to_port = self.topo_access_link
        for link in link_to_port.keys():
            (src_dpid, dst_dpid) = link
            link_info = link_to_port[link]
            try:
                if len(link_info) > 5:
                    local_port = link_info[5]
                    if dst_dpid in free_bandwidth and local_port in free_bandwidth[dst_dpid]:
                        local_free_bandwidth, _ = free_bandwidth[dst_dpid][local_port]
                        self.topo_access_link[link][3] = local_free_bandwidth
                        self.graph[src_dpid][dst_dpid]['free_bandwith'] = local_free_bandwidth
                else:
                    local_port = link_info[0]
                    if src_dpid in free_bandwidth and local_port in free_bandwidth[src_dpid]:
                        local_free_bandwidth, _ = free_bandwidth[src_dpid][local_port]
                        self.topo_access_link[link][3] = local_free_bandwidth
                        self.graph[src_dpid][dst_dpid]['free_bandwith'] = local_free_bandwidth
            except Exception: pass

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def _port_stats_reply_handler(self, ev):
        body = ev.msg.body
        dpid = ev.msg.datapath.id
        self.free_bandwidth.setdefault(dpid, {})
        self.port_loss_stats.setdefault(dpid, {})
        now_timestamp = time.time()

        for stat in sorted(body, key=attrgetter('port_no')):
            port_no = stat.port_no
            if port_no != ofproto_v1_3.OFPP_LOCAL:
                key = (dpid, port_no)
                value = (stat.tx_packets, stat.rx_packets, stat.tx_bytes, stat.rx_bytes, 
                        stat.rx_dropped, stat.tx_dropped, now_timestamp)
                self._save_stats(self.port_stats, key, value, 5)

                if key[0] in self.port_loss_stats and key[1] in self.port_loss_stats[key[0]]:
                    prev_rx_dropped, prev_tx_dropped = self.port_loss_stats[key[0]][key[1]]
                    prev_stats = self.port_stats[key][-2]
                    rx_packets_delta = stat.rx_packets - prev_stats[1]
                    tx_packets_delta = stat.tx_packets - prev_stats[0]
                    rx_dropped_delta = stat.rx_dropped - prev_rx_dropped
                    tx_dropped_delta = stat.tx_dropped - prev_tx_dropped
                    rx_loss_rate = 0.0
                    tx_loss_rate = 0.0
                    if rx_packets_delta + rx_dropped_delta > 0:
                        rx_loss_rate = float(rx_dropped_delta) / (rx_packets_delta + rx_dropped_delta)
                    if tx_packets_delta + tx_dropped_delta > 0:
                        tx_loss_rate = float(tx_dropped_delta) / (tx_packets_delta + tx_dropped_delta)
                    loss_rate = max(rx_loss_rate, tx_loss_rate)
                    self._update_link_loss_rate(dpid, port_no, loss_rate)

                self.port_loss_stats[key[0]][key[1]] = (stat.rx_dropped, stat.tx_dropped)
                port_stats = self.port_stats[key]
                if len(port_stats) > 1:
                    curr_stat = port_stats[-1][2]
                    prev_stat = port_stats[-2][2]
                    period = self._get_period(port_stats[-1][6], port_stats[-2][6])
                    speed = self._cal_speed(curr_stat, prev_stat, period)
                    self._save_freebandwidth(dpid, port_no, speed)

    def _detector(self):
        while True:
            self._send_echo_request()
            self.add_delay_info()
            hub.sleep(1)

    def _send_echo_request(self):
        datapaths = list(self.dpid_to_switch.values())
        for datapath in datapaths:
            parser = datapath.ofproto_parser
            data_time = "%.12f" % time.time()
            byte_arr = bytearray(data_time.encode())
            echo_req = parser.OFPEchoRequest(datapath, data=byte_arr)
            datapath.send_msg(echo_req)
            hub.sleep(0.5)

    def add_delay_info(self):
        for link in list(self.topo_inter_link.keys()):
            (src_dpid, dst_dpid) = link
            try:
                delay = self._get_delay(src_dpid, dst_dpid)
                self.topo_inter_link[(src_dpid, dst_dpid)][2] = delay
                if self.graph.has_edge(src_dpid, dst_dpid):
                    self.graph[src_dpid][dst_dpid]['delay'] = delay
            except Exception: pass

        for link in list(self.topo_access_link.keys()):
            (local_dpid, remote_dpid) = link
            try:
                delay = self._get_access_delay(local_dpid, remote_dpid)
                self.topo_access_link[(local_dpid, remote_dpid)][2] = delay
                if self.graph.has_edge(local_dpid, remote_dpid):
                    self.graph[local_dpid][remote_dpid]['delay'] = delay
            except Exception: pass

    def _get_delay(self, src, dst):
        try:
            if (src, dst) not in self.lldp_delay: return float(0)
            fwd_delay = self.lldp_delay[(src, dst)][0]
            if src not in self.echo_latency: return float(0)
            src_latency = self.echo_latency[src]
            dst_latency = self.lldp_delay[(src, dst)][1]
            delay = fwd_delay - (src_latency + dst_latency) / 2
            return max(delay, 0)
        except: return float(0)

    def _get_access_delay(self, src, dst):
        try:
            if (src, dst) in self.lldp_delay:
                fwd_delay = self.lldp_delay[(src, dst)][0]
                src_echodelay = self.lldp_delay[(src, dst)][1]
            elif (dst, src) in self.lldp_delay:
                fwd_delay = self.lldp_delay[(dst, src)][0]
                src_echodelay = self.lldp_delay[(dst, src)][1]
            else:
                return float('inf')
            if src not in self.echo_latency: return float('inf')
            src_latency = self.echo_latency[src]
            dst_latency = src_echodelay
            delay = fwd_delay - (src_latency + dst_latency) / 2
            return max(delay, 0)
        except: return float('inf')

    def _save_lldp_delay(self, src=0, dst=0, lldpdelay=0, echodelay=0):
        self.lldp_delay[(src, dst)] = [lldpdelay, echodelay]
    
    def _send_lldp_report_to_server(self, src_dpid, src_port_no, dst_dpid, dst_inport,
                                    send_time, echodelay_src, receive_time):
        if not self.is_connected: return
        dst_echo = self.echo_latency.get(dst_dpid, 0.0)
        report_msg = {
            "type": "lldp_report",
            "src_dpid": src_dpid,
            "src_port_no": src_port_no,
            "dst_dpid": dst_dpid,
            "dst_inport": dst_inport,
            "send_time": send_time,
            "receive_time": receive_time,
            "src_echo": echodelay_src,
            "dst_echo": dst_echo
        }
        self._send_to_server(report_msg)
    
    def _handle_portdata_query(self, query_msg):
        src_dpid = query_msg.get('src_dpid')
        src_port_no = query_msg.get('src_port_no')
        request_id = query_msg.get('request_id')
        timestamp = None
        echodelay = 0.0
        if self.switches is not None:
            for port_obj in self.switches.ports.keys():
                if src_dpid == port_obj.dpid and src_port_no == port_obj.port_no:
                    port_data = self.switches.ports[port_obj]
                    timestamp = port_data.timestamp
                    echodelay = getattr(port_data, 'echo_delay', 0.0)
                    break
        response_msg = {
            "type": "portdata_response",
            "request_id": request_id,
            "src_dpid": src_dpid,
            "src_port_no": src_port_no,
            "timestamp": timestamp,
            "echodelay": echodelay,
            "status": "ok" if timestamp is not None else "not_found"
        }
        self._send_to_server(response_msg)
    
    def _handle_portdata_response(self, response_msg):
        request_id = response_msg.get('request_id')
        src_dpid = response_msg.get('src_dpid')
        timestamp = response_msg.get('timestamp')
        echodelay = response_msg.get('echodelay', 0.0)
        status = response_msg.get('status')
        query_key = None
        for key in self.pending_portdata_queries.keys():
            if str(key) == request_id:
                query_key = key
                break
        if query_key is None: return
        query_data = self.pending_portdata_queries.pop(query_key, None)
        if query_data is None: return
        lldp_receive_time, query_time = query_data
        dst_dpid = query_key[2]
        if status == "ok" and timestamp is not None:
            lldpdelay = lldp_receive_time - timestamp
            self._save_lldp_delay(src=dst_dpid, dst=src_dpid, lldpdelay=lldpdelay, echodelay=echodelay)

    def _handle_lldp_delay_update(self, response_msg):
        if response_msg.get('status', 'ok') != 'ok': return
        src_dpid = response_msg.get('src_dpid')
        dst_dpid = response_msg.get('dst_dpid')
        lldp_delay = response_msg.get('fwd_delay', 0.0)
        src_echo = response_msg.get('src_echo', 0.0)
        calc_delay = response_msg.get('delay', 0.0)
        if src_dpid is None or dst_dpid is None: return
        self._save_lldp_delay(src=dst_dpid, dst=src_dpid, lldpdelay=lldp_delay, echodelay=src_echo)
        try:
            if (dst_dpid, src_dpid) in self.topo_access_link:
                self.topo_access_link[(dst_dpid, src_dpid)][2] = calc_delay
                self.graph[dst_dpid][src_dpid]['delay'] = calc_delay
        except Exception: pass

    @set_ev_cls(ofp_event.EventOFPEchoReply, MAIN_DISPATCHER)
    def _echo_reply_handler(self, ev):
        now_timestamp = time.time()
        try:
            latency = now_timestamp - eval(ev.msg.data)
            self.echo_latency[ev.msg.datapath.id] = latency
            self.echo_timestamp[ev.msg.datapath.id] = now_timestamp
        except: return

    def show(self):
        while True:
            print("\n" + "="*80)
            print(f"控制器拓扑状态 - {time.strftime('%H:%M:%S')}")
            print("="*80)
            print(f"\n【交换机】共 {len(self.dpid_to_switch)} 个")
            for dpid in self.dpid_to_switch.keys():
                print(f"  - 交换机 {dpid}")
            
            print(f"\n【域内链路】共 {len(self.topo_inter_link)} 条")
            for (src, dst), info in self.topo_inter_link.items():
                print(f"  - {src} -> {dst} | Port:{info[0]} | Delay:{info[2]:.3f}ms")
            
            print(f"\n【域间链路】共 {len(self.topo_access_link)} 条")
            for (src, dst), info in self.topo_access_link.items():
                local_port = info[5] if len(info) > 5 else (info[0] if dst == self.dpid_to_switch else "Unknown")
                print(f"  - Remote:{src} -> Local:{dst} | BW:{info[3]:.2f}Mbps")
            
            print(f"\n【主机】")
            for dpid, ports in self.host_to_sw_port.items():
                for port, hosts in ports.items():
                    for mac, ip in hosts:
                        print(f"  - SW:{dpid} Port:{port} -> MAC:{mac} IP:{ip}")
            print("\n" + "="*80)
            hub.sleep(5)

    def get_path(self, src, dst):
        if src == dst: return [src]
        try:
            path = nx.shortest_path(self.graph, src, dst)
            return path
        except: return []

    def get_port(self, dpid, port_no):
        if port_no in self.switch_mac_to_port[dpid].keys(): return True
        return False

    def add_flow(self, datapath, priority, match, actions, proto=0, hard_timeout=0, idle_timeout=0, buffer_id=None):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
        if buffer_id:
            mod = parser.OFPFlowMod(datapath=datapath, buffer_id=buffer_id, priority=priority,
                                    idle_timeout=idle_timeout, hard_timeout=hard_timeout,
                                    match=match, instructions=inst)
        else:
            mod = parser.OFPFlowMod(datapath=datapath, priority=priority, idle_timeout=idle_timeout,
                                    hard_timeout=hard_timeout, match=match, instructions=inst)
        datapath.send_msg(mod)
    
    def del_flow(self, datapath):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        mod = parser.OFPFlowMod(datapath=datapath, command=ofproto.OFPFC_DELETE,
                                out_port=ofproto.OFPP_ANY, out_group=ofproto.OFPG_ANY)
        datapath.send_msg(mod)

    def send_packet_to_outport(self, datapath, msg, in_port, actions):
        data = None
        if msg.buffer_id == datapath.ofproto.OFP_NO_BUFFER:
            data = msg.data
        out = datapath.ofproto_parser.OFPPacketOut(datapath=datapath, buffer_id=msg.buffer_id,
                                                   in_port=in_port, actions=actions, data=data)
        datapath.send_msg(out)

    def get_switch_id_by_ip(self, ip_address):
        for switch_id in self.host_to_sw_port:
            for port in self.host_to_sw_port[switch_id]:
                for host in self.host_to_sw_port[switch_id][port]:
                    if host[1] == ip_address: return switch_id
    
    def get_switch_port_by_ip(self, ip_address):
        for switch_id in self.host_to_sw_port:
            for port in self.host_to_sw_port[switch_id]:
                for host in self.host_to_sw_port[switch_id][port]:
                    if host[1] == ip_address: return port
    
    def get_mac_by_ip(self, ip_address):
        for switch_id in self.host_to_sw_port:
            for port in self.host_to_sw_port[switch_id]:
                for host in self.host_to_sw_port[switch_id][port]:
                    if host[1] == ip_address: return host[0]

    def get_port_from_link(self, dpid, next_id):
        if (dpid, next_id) in self.topo_inter_link.keys():
            return self.topo_inter_link[(dpid, next_id)][0]
        if (dpid, next_id) in self.topo_access_link.keys():
            return self.topo_access_link[(dpid, next_id)][0]
            
    def install_flow_entry(self, path, src_ip, dst_ip, port=None, msg=None):
        self.logger.info("【流表】安装: 路径=%s, %s -> %s", path, src_ip, dst_ip)
        num = len(path)
        if num == 1:
            dpid = path[0]
            datapath = self.dpid_to_switch[dpid]
            in_port = port
            dst_port = self.get_switch_port_by_ip(dst_ip)
            dst_mac = self.get_mac_by_ip(dst_ip)
            src_mac = self.get_mac_by_ip(src_ip)
            if not dst_port: return
            
            actions = [datapath.ofproto_parser.OFPActionSetField(eth_dst=dst_mac),
                      datapath.ofproto_parser.OFPActionOutput(dst_port)]
            match = datapath.ofproto_parser.OFPMatch(eth_type=ether.ETH_TYPE_IP,
                                                   in_port=in_port, ipv4_dst=dst_ip, ipv4_src=src_ip)
            self.add_flow(datapath, 1, match, actions)
            
            actions_rev = [datapath.ofproto_parser.OFPActionSetField(eth_dst=src_mac),
                          datapath.ofproto_parser.OFPActionOutput(in_port)]
            match_rev = datapath.ofproto_parser.OFPMatch(eth_type=ether.ETH_TYPE_IP,
                                                       in_port=dst_port, ipv4_dst=src_ip, ipv4_src=dst_ip)
            self.add_flow(datapath, 1, match_rev, actions_rev)
            
            if msg: self.send_packet_to_outport(datapath, msg, in_port, actions)
            
        else:
            for i in range(1, len(path)-1):
                dpid = path[i]
                if dpid in self.dpid_to_switch:
                    datapath = self.dpid_to_switch[dpid]
                    if i == 1:
                        in_port = self.get_switch_port_by_ip(src_ip)
                        src_mac = self.get_mac_by_ip(src_ip)
                    else:
                        in_port = self.get_port_from_link(dpid, path[i-1])
                    
                    if i == len(path)-2:
                        out_port = self.get_switch_port_by_ip(dst_ip)
                        dst_mac = self.get_mac_by_ip(dst_ip)
                        actions = [datapath.ofproto_parser.OFPActionSetField(eth_dst=dst_mac),
                                  datapath.ofproto_parser.OFPActionOutput(out_port)]
                    else:
                        out_port = self.get_port_from_link(dpid, path[i+1])
                        actions = [datapath.ofproto_parser.OFPActionOutput(out_port)]
                    
                    match = datapath.ofproto_parser.OFPMatch(eth_type=ether.ETH_TYPE_IP,
                                                           in_port=in_port, ipv4_dst=dst_ip, ipv4_src=src_ip)
                    self.add_flow(datapath, 1, match, actions)
                    
                    if i == 1:
                        actions_rev = [datapath.ofproto_parser.OFPActionSetField(eth_dst=src_mac),
                                      datapath.ofproto_parser.OFPActionOutput(in_port)]
                    else:
                        actions_rev = [datapath.ofproto_parser.OFPActionOutput(in_port)]
                        
                    match_rev = datapath.ofproto_parser.OFPMatch(eth_type=ether.ETH_TYPE_IP,
                                                               in_port=out_port, ipv4_dst=src_ip, ipv4_src=dst_ip)
                    self.add_flow(datapath, 1, match_rev, actions_rev)
                    
                    if msg and i == 1:
                        self.send_packet_to_outport(datapath, msg, in_port, actions)

    def _add_switch_map(self, sw):
        dpid = sw.dp.id
        self.switch_mac_to_port.setdefault(dpid, {})
        self.host_to_sw_port.setdefault(dpid, {})
        self.mac_to_port.setdefault(dpid, {})
        if dpid not in self.dpid_to_switch:
            self.dpid_to_switch[dpid] = sw.dp
            self.dpid_to_switch_ip[dpid] = sw.dp.address
            for p in sw.ports:
                self.switch_mac_to_port[dpid][p.port_no] = p.hw_addr

    def _delete_switch_map(self, sw):
        if sw.dp.id in self.dpid_to_switch:
            self.host_to_sw_port.pop(sw.dp.id,0)
            self.switch_mac_to_port.pop(sw.dp.id,0)
            self.mac_to_port.pop(sw.dp.id,0)
            self.dpid_to_switch.pop(sw.dp.id,0)
            self.dpid_to_switch_ip.pop(sw.dp.id,0)
            self.echo_timestamp.pop(sw.dp.id,0)

    def _update_switch_map(self, sw):
        dpid = sw.dp.id
        if dpid not in self.dpid_to_switch:
            self.switch_mac_to_port.setdefault(dpid, {})
            self.host_to_sw_port.setdefault(dpid, {})
            self.mac_to_port.setdefault(dpid, {})
            self.dpid_to_switch_ip[dpid] = sw.dp.address
            self.dpid_to_switch[dpid] = sw.dp
            self.echo_timestamp[dpid] = time.time()
            for p in sw.ports:
                self.switch_mac_to_port[dpid][p.port_no] = p.hw_addr

    def delete_switch(self, dpid):
        if dpid in self.dpid_to_switch:
            datapath = self.dpid_to_switch[dpid]
            try:
                datapath.socket.close()
            except: pass

    def _check_switch_state(self, echo_timestamp):
        while True:
            curr_time = time.time()
            for dpid in list(echo_timestamp.keys()):
                if (curr_time - echo_timestamp[dpid]) > 70:
                    echo_timestamp.pop(dpid, 0)
                    hub.spawn(self.delete_switch, dpid)
            hub.sleep(5)

    def add_inter_link(self, link):
        src_dpid, dst_dpid = link.src.dpid, link.dst.dpid
        src_port, dst_port = link.src.port_no, link.dst.port_no
        if (src_dpid, dst_dpid) not in self.topo_inter_link:
            self.topo_inter_link[(src_dpid, dst_dpid)] = [src_port, 0, 0, 0, 0]
            self.graph.add_edge(src_dpid, dst_dpid)
        if (dst_dpid, src_dpid) not in self.topo_inter_link:
            self.topo_inter_link[(dst_dpid, src_dpid)] = [dst_port, 0, 0, 0, 0]
            self.graph.add_edge(dst_dpid, src_dpid)

    def delete_inter_link(self, link):
        src_dpid, dst_dpid = link.src.dpid, link.dst.dpid
        if (src_dpid, dst_dpid) in self.topo_inter_link:
            del self.topo_inter_link[(src_dpid, dst_dpid)]
            if self.graph.has_edge(src_dpid, dst_dpid): self.graph.remove_edge(src_dpid, dst_dpid)
        if (dst_dpid, src_dpid) in self.topo_inter_link:
            del self.topo_inter_link[(dst_dpid, src_dpid)]
            if self.graph.has_edge(dst_dpid, src_dpid): self.graph.remove_edge(dst_dpid, src_dpid)

    def link_timeout_detection(self, access_link):
        while True:
            now_timestamp = time.time()
            links_to_remove = []
            for (src, dst) in list(access_link.keys()):
                if (now_timestamp - access_link[(src, dst)][1]) > 70:
                    links_to_remove.append((src, dst))
            for (src, dst) in links_to_remove:
                access_link.pop((src, dst))
                if self.graph.has_edge(src, dst):
                    self.graph.remove_edge(src, dst)
            hub.sleep(3)

    @set_ev_cls([event.EventSwitchEnter])
    def _switch_enter_handle(self, ev):
        self._add_switch_map(ev.switch)

    @set_ev_cls([event.EventSwitchReconnected])
    def _switch_reconnected_handle(self, ev):
        self._update_switch_map(ev.switch)

    @set_ev_cls([event.EventSwitchLeave])
    def _switch_leave_handle(self, ev):
        self._delete_switch_map(ev.switch)

    @set_ev_cls([event.EventLinkAdd])
    def add_link(self, ev):
        self.add_inter_link(ev.link)

    @set_ev_cls([event.EventLinkDelete])
    def delete_link(self, ev):
        self.delete_inter_link(ev.link)

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        self.del_flow(datapath)
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER, ofproto.OFPCML_NO_BUFFER)]
        self.add_flow(datapath, 0, match, actions)
        match_lldp = parser.OFPMatch(eth_type=ether_types.ETH_TYPE_LLDP)
        actions_lldp = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER, ofproto.OFPCML_NO_BUFFER)]
        self.add_flow(datapath, 65535, match_lldp, actions_lldp)

    def _update_link_loss_rate(self, dpid, port_no, loss_rate):
        for link in self.topo_inter_link:
            if link[0] == dpid and self.topo_inter_link[link][0] == port_no:
                self.topo_inter_link[link][4] = loss_rate
                if link[0] in self.graph and link[1] in self.graph[link[0]]:
                    self.graph[link[0]][link[1]]['loss_rate'] = loss_rate
                break
        for link in self.topo_access_link:
            if link[0] == dpid and self.topo_access_link[link][0] == port_no:
                self.topo_access_link[link][4] = loss_rate
                if link[0] in self.graph and link[1] in self.graph[link[0]]:
                    self.graph[link[0]][link[1]]['loss_rate'] = loss_rate
                break

    def _connect_to_server(self):
        while True:
            try:
                if not self.is_connected:
                    self.logger.info("尝试连接到server_agent...")
                    self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    self.server_socket.connect(self.server_addr)
                    self.is_connected = True
                    self.logger.info("成功连接到server_agent")
                    hub.spawn(self._receive_from_server)
            except Exception:
                if self.server_socket: self.server_socket.close()
                self.is_connected = False
            hub.sleep(SERVER_CONFIG['reconnect_interval'])

    def _send_topo_loop(self):
        while True:
            if self.is_connected:
                try:
                    local_switches = set(self.dpid_to_switch.keys())
                    filtered_host_info = []
                    for dpid, ports in self.host_to_sw_port.items():
                        if dpid not in local_switches: continue
                        for port, hosts in ports.items():
                            if self.is_link_port(dpid, port): continue
                            for host in hosts:
                                if host[1] != "0.0.0.0":
                                    filtered_host_info.append({'dpid': dpid, 'port': port, 'mac': host[0], 'ip': host[1]})
                    
                    link_info = []
                    for link in self.topo_inter_link.keys():
                        link_info.append({
                            'src': link[0], 'dst': link[1],
                            'src_port': self.topo_inter_link[link][0],
                            'delay': self.topo_inter_link[link][2],
                            'bw': self.topo_inter_link[link][3],
                            'loss': self.topo_inter_link[link][4],
                            'type': 'intra'
                        })
                    for link in self.topo_access_link.keys():
                        link_info.append({
                            'src': link[0], 'dst': link[1],
                            'src_port': self.topo_access_link[link][0],
                            'delay': self.topo_access_link[link][2],
                            'bw': self.topo_access_link[link][3],
                            'loss': self.topo_access_link[link][4],
                            'type': 'inter'
                        })
                    
                    topo_msg = {
                        "type": "topo",
                        "switches": list(self.dpid_to_switch.keys()),
                        "link": link_info,
                        "host": filtered_host_info
                    }
                    self._send_to_server(topo_msg)
                except Exception:
                    self.is_connected = False
            hub.sleep(10)

    def _send_to_server(self, msg):
        if self.is_connected:
            try:
                data = json.dumps(msg) + '\n'
                self.server_socket.sendall(data.encode())
            except Exception:
                self.is_connected = False
                if self.server_socket: self.server_socket.close()

    def _heartbeat_loop(self):
        while True:
            try:
                if self.is_connected: self._send_to_server({"type": "heartbeat"})
            except Exception: self.is_connected = False
            finally: hub.sleep(2)

    def _receive_from_server(self):
        buffer = ""
        while self.is_connected:
            try:
                data = self.server_socket.recv(4096)
                if not data: break
                buffer += data.decode('utf-8')
                while '\n' in buffer:
                    line, buffer = buffer.split('\n', 1)
                    if line.strip():
                        try:
                            msg = json.loads(line)
                            self._handle_server_msg(msg)
                        except: pass
            except Exception: break
        self.is_connected = False
        if self.server_socket: self.server_socket.close()

    def _handle_server_msg(self, msg):
        if not isinstance(msg, dict): return
        msg_type = msg.get('type')
        if msg_type == 'portdata_query': self._handle_portdata_query(msg)
        elif msg_type == 'portdata_response': self._handle_portdata_response(msg)
        elif msg_type == 'lldp_delay_update': self._handle_lldp_delay_update(msg)
        elif msg.get('status') == 'ok' and 'path' in msg:
            path = msg['path']
            if path: self._process_path(path, msg.get('src_ip'), msg.get('dst_ip'))

    def _process_path(self, path, src_ip, dst_ip, msg=None):
        for i in range(1, len(path) - 1):
            dpid = path[i]
            if dpid in self.dpid_to_switch:
                datapath = self.dpid_to_switch[dpid]
                if i == 1:
                    in_port = self.get_switch_port_by_ip(src_ip)
                    src_mac_addr = self.get_mac_by_ip(src_ip)
                else:
                    in_port = self.get_port_from_link(dpid, path[i-1])
                
                if i == len(path) - 2:
                    out_port = self.get_switch_port_by_ip(dst_ip)
                    dst_mac_addr = self.get_mac_by_ip(dst_ip)
                    actions = [datapath.ofproto_parser.OFPActionSetField(eth_dst=dst_mac_addr),
                              datapath.ofproto_parser.OFPActionOutput(out_port)]
                else:
                    out_port = self.get_port_from_link(dpid, path[i+1])
                    actions = [datapath.ofproto_parser.OFPActionOutput(out_port)]
                
                match = datapath.ofproto_parser.OFPMatch(eth_type=ether.ETH_TYPE_IP,
                                                       in_port=in_port, ipv4_dst=dst_ip, ipv4_src=src_ip)
                self.add_flow(datapath, 1, match, actions)
                
                if i == 1:
                    src_mac_addr = self.get_mac_by_ip(src_ip)
                    actions_rev = [datapath.ofproto_parser.OFPActionSetField(eth_dst=src_mac_addr),
                                  datapath.ofproto_parser.OFPActionOutput(in_port)]
                else:
                    actions_rev = [datapath.ofproto_parser.OFPActionOutput(in_port)]
                
                match_rev = datapath.ofproto_parser.OFPMatch(eth_type=ether.ETH_TYPE_IP,
                                                           in_port=out_port, ipv4_dst=src_ip, ipv4_src=dst_ip)
                self.add_flow(datapath, 1, match_rev, actions_rev)
                
                if msg and i == 1:
                    self.send_packet_to_outport(datapath, msg, in_port, actions)

    def _request_path(self, src_ip, dst_ip, dpid, in_port, msg):
        path_msg = {
            "type": "path_request", "src": src_ip, "dst": dst_ip,
            "switch_id": dpid, "in_port": in_port
        }
        self._send_to_server(path_msg)

    def _cleanup_pending_host_learning(self):
        # 兜底清理（虽然现在直接学习了，但保留该线程防止有残留逻辑需要）
        pass
