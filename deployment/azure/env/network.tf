# One network. The Container Apps environment and PostgreSQL are in delegated subnets; the engine and
# the GPU machine have private addresses only, reachable from the apps subnet and nowhere else. Key
# Vault and storage are reached through service endpoints from the subnets that need them, and from
# the operator's addresses (var.operator_ip_ranges); everything else is refused.

resource "azurerm_virtual_network" "main" {
  name                = "${local.name_prefix}-vnet"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  address_space       = [local.cidr.vnet]
  tags                = local.common_tags
}

resource "azurerm_subnet" "apps" {
  name                 = "snet-apps"
  resource_group_name  = azurerm_resource_group.main.name
  virtual_network_name = azurerm_virtual_network.main.name
  address_prefixes     = [local.cidr.apps]
  service_endpoints    = ["Microsoft.KeyVault", "Microsoft.Storage"]

  delegation {
    name = "container-apps"
    service_delegation {
      name    = "Microsoft.App/environments"
      actions = ["Microsoft.Network/virtualNetworks/subnets/join/action"]
    }
  }
}

resource "azurerm_subnet" "data" {
  name                 = "snet-data"
  resource_group_name  = azurerm_resource_group.main.name
  virtual_network_name = azurerm_virtual_network.main.name
  address_prefixes     = [local.cidr.data]

  delegation {
    name = "postgres-flexible"
    service_delegation {
      name    = "Microsoft.DBforPostgreSQL/flexibleServers"
      actions = ["Microsoft.Network/virtualNetworks/subnets/join/action"]
    }
  }
}

resource "azurerm_subnet" "engine" {
  name                 = "snet-engine"
  resource_group_name  = azurerm_resource_group.main.name
  virtual_network_name = azurerm_virtual_network.main.name
  address_prefixes     = [local.cidr.engine]
}

resource "azurerm_subnet" "inference" {
  name                 = "snet-inference"
  resource_group_name  = azurerm_resource_group.main.name
  virtual_network_name = azurerm_virtual_network.main.name
  address_prefixes     = [local.cidr.inference]
  service_endpoints    = ["Microsoft.Storage"] # model weights cache, if ever moved to storage
}

# The engine answers queries (8080) and takes its application (19071) from the apps subnet only.
resource "azurerm_network_security_group" "engine" {
  name                = "${local.name_prefix}-engine-nsg"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  tags                = local.common_tags

  security_rule {
    name                       = "from-apps"
    priority                   = 100
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_address_prefix      = local.cidr.apps
    source_port_range          = "*"
    destination_address_prefix = local.cidr.engine
    destination_port_ranges    = ["8080", "19071"]
  }
  security_rule {
    name                       = "nothing-else"
    priority                   = 4000
    direction                  = "Inbound"
    access                     = "Deny"
    protocol                   = "*"
    source_address_prefix      = "*"
    source_port_range          = "*"
    destination_address_prefix = "*"
    destination_port_range     = "*"
  }
}

# The models answer embeddings (8080) and reranking (8081) from the apps subnet only.
resource "azurerm_network_security_group" "inference" {
  name                = "${local.name_prefix}-inference-nsg"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  tags                = local.common_tags

  security_rule {
    name                       = "from-apps"
    priority                   = 100
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_address_prefix      = local.cidr.apps
    source_port_range          = "*"
    destination_address_prefix = local.cidr.inference
    destination_port_ranges    = ["8080", "8081"]
  }
  security_rule {
    name                       = "nothing-else"
    priority                   = 4000
    direction                  = "Inbound"
    access                     = "Deny"
    protocol                   = "*"
    source_address_prefix      = "*"
    source_port_range          = "*"
    destination_address_prefix = "*"
    destination_port_range     = "*"
  }
}

resource "azurerm_subnet_network_security_group_association" "engine" {
  subnet_id                 = azurerm_subnet.engine.id
  network_security_group_id = azurerm_network_security_group.engine.id
}

resource "azurerm_subnet_network_security_group_association" "inference" {
  subnet_id                 = azurerm_subnet.inference.id
  network_security_group_id = azurerm_network_security_group.inference.id
}

# The two machines reach the internet only to install packages and fetch the images and model weights.
resource "azurerm_public_ip" "egress" {
  name                = "${local.name_prefix}-egress-ip"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  allocation_method   = "Static"
  sku                 = "Standard"
  tags                = local.common_tags
}

resource "azurerm_nat_gateway" "egress" {
  name                = "${local.name_prefix}-nat"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  sku_name            = "Standard"
  tags                = local.common_tags
}

resource "azurerm_nat_gateway_public_ip_association" "egress" {
  nat_gateway_id       = azurerm_nat_gateway.egress.id
  public_ip_address_id = azurerm_public_ip.egress.id
}

resource "azurerm_subnet_nat_gateway_association" "engine" {
  subnet_id      = azurerm_subnet.engine.id
  nat_gateway_id = azurerm_nat_gateway.egress.id
}

resource "azurerm_subnet_nat_gateway_association" "inference" {
  subnet_id      = azurerm_subnet.inference.id
  nat_gateway_id = azurerm_nat_gateway.egress.id
}

resource "azurerm_private_dns_zone" "postgres" {
  name                = "${local.name_prefix}.private.postgres.database.azure.com"
  resource_group_name = azurerm_resource_group.main.name
  tags                = local.common_tags
}

resource "azurerm_private_dns_zone_virtual_network_link" "postgres" {
  name                  = "postgres-link"
  resource_group_name   = azurerm_resource_group.main.name
  private_dns_zone_name = azurerm_private_dns_zone.postgres.name
  virtual_network_id    = azurerm_virtual_network.main.id
  tags                  = local.common_tags
}
